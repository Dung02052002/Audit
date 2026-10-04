"""G-077 Provenance Record: the entity.

Rules the user approved on 2026-10-04: a record of the source and licence of an
asset with optional details (a URL, a retrieval time, a licence name, a licence
URL and reference, attribution, owner, a checksum, a proof) and at least one of
them; text is collapsed, never turned into None, URLs hold no credentials, no
secret-looking query or fragment parameter, no bad port and no whitespace, an
error message never holds a value, the checksum is lowercase hex, and the
retrieval time is UTC and not after the time of recording.
"""

import dataclasses
import unicodedata
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content import provenance
from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.providers import research

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
AI = Actor(ActorKind.AI, "mock/mock-1")
SHA = "ab" * 32
TEXT_FIELDS = {
    "license_name": provenance.MAX_LICENSE_NAME,
    "license_ref": provenance.MAX_LICENSE_REF,
    "attribution": provenance.MAX_ATTRIBUTION,
    "owner": provenance.MAX_OWNER,
    "proof": provenance.MAX_PROOF,
}
CONTENT_FIELDS = provenance.CONTENT_FIELDS
URL_FIELDS = ["source_url", "license_url"]
SECRET = "SECRET"  # a word no error message may hold
LISTED_SECRET_NAMES = [
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "auth",
    "authorization",
    "key",
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "pwd",
    "sig",
    "signature",
    "session",
    "sessionid",
    "credential",
    "credentials",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
    "x-goog-signature",
    "x-goog-credential",
]
# URLs that are refused and hold SECRET: no message may repeat it.
BAD_URLS_WITH_A_SECRET = [
    "ftp://user:SECRET@host/x",
    "http://user:SECRET@/x",
    "http:\\\\user:SECRET@host",
    "http://user:SECRET＠host/x",  # a fullwidth at sign: urlsplit raises
    "https://user:SECRET@stock.example/logo",
    "https://:SECRET@stock.example/",
    "http://user:SECRET%40host.com/",  # the "port" is not a number
    "http://host:SECRET/",
    "https://stock.example/a?token=SECRET",
    "https://stock.example/a?mode=1&API_KEY=SECRET",
    "https://stock.example/a#access_token=SECRET",
    "https://stock.example/a#/route?password=SECRET",
    "https://stock.example/a b?x=SECRET",
]


def make(**overrides) -> Provenance:
    arguments = {"source_url": "https://stock.example/logo", "clock": lambda: T0}
    arguments |= overrides
    return Provenance.create(
        "a1", recorded_by=arguments.pop("recorded_by", USER), **arguments
    )


def alone(name: str, value) -> Provenance:
    """A record whose only detail is ``name``."""
    return make(**({"source_url": None} | {name: value}))


def test_the_limits_are_the_documented_constants() -> None:
    assert provenance.MAX_URL_LENGTH == research.MAX_URL_LENGTH == 2048
    assert TEXT_FIELDS == {
        "license_name": 200,
        "license_ref": 500,
        "attribution": 500,
        "owner": 200,
        "proof": 1000,
    }


def test_a_full_record_is_built_and_keeps_its_details() -> None:
    retrieved = T0 - timedelta(days=2)
    made = make(
        source_url="https://stock.example/logo.png",
        retrieved_at=retrieved,
        license_name="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        license_ref="CC-BY-4.0",
        attribution="Photo by Lan",
        owner="Lan",
        file_sha256=SHA,
        proof="Invoice 42 in the shared drive",
        recorded_by=AI,
    )

    assert made.id
    assert made.asset_id == "a1"
    assert made.recorded_by == AI
    assert made.created_at == T0
    assert made.retrieved_at == retrieved
    assert made.file_sha256 == SHA


def test_each_detail_alone_is_a_valid_record() -> None:
    details = {
        "source_url": "http://a.example/x",
        "retrieved_at": T0,
        "license_name": "MIT",
        "license_url": "https://a.example/license",
        "license_ref": "ref",
        "attribution": "credit",
        "owner": "Lan",
        "file_sha256": SHA,
        "proof": "note",
    }
    assert set(details) == set(CONTENT_FIELDS)
    for name, value in details.items():
        made = alone(name, value)
        assert getattr(made, name) == value


def test_text_is_stripped_and_collapsed() -> None:
    made = make(
        license_name="  CC \n BY\t4.0 ",
        license_ref=" a   b ",
        attribution="  Photo   by  Lan",
        owner="\tLan  Nguyen ",
        proof=" kept \n in   the drive ",
    )

    assert made.license_name == "CC BY 4.0"
    assert made.license_ref == "a b"
    assert made.attribution == "Photo by Lan"
    assert made.owner == "Lan Nguyen"
    assert made.proof == "kept in the drive"


def test_text_is_not_unicode_normalised() -> None:
    nfd = unicodedata.normalize("NFD", "Nguyễn Văn A")

    assert make(owner=nfd).owner == nfd


@pytest.mark.parametrize("name", TEXT_FIELDS)
@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_an_empty_text_is_an_error_not_none(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        make(**{name: value})


@pytest.mark.parametrize("name", TEXT_FIELDS)
def test_a_text_of_exactly_the_limit_is_accepted_and_one_more_fails(
    name: str,
) -> None:
    limit = TEXT_FIELDS[name]

    assert getattr(make(**{name: "x" * limit}), name) == "x" * limit
    with pytest.raises(ValueError, match=name):
        make(**{name: "x" * (limit + 1)})


def test_the_limit_counts_the_stripped_and_collapsed_text() -> None:
    padded = "  " + "x" * 250 + " \n\t " + "y" * 249 + "  "

    assert len(padded) > 500
    assert len(make(license_ref=padded).license_ref) == 500
    with pytest.raises(ValueError, match="license_ref"):
        make(license_ref=padded + "z")


@pytest.mark.parametrize("name", ["source_url", "license_url"])
def test_urls_are_stripped_and_stored_as_given(name: str) -> None:
    made = alone(name, "  HTTPS://Stock.Example/A?b=1#frag \n")

    assert getattr(made, name) == "HTTPS://Stock.Example/A?b=1#frag"


@pytest.mark.parametrize("name", ["source_url", "license_url"])
@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "stock.example/logo",
        "ftp://stock.example/logo",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://",
        "//stock.example/logo",
        "/relative/path",
    ],
)
def test_a_malformed_url_is_refused(name: str, url: str) -> None:
    with pytest.raises(ValueError, match=name):
        alone(name, url)


@pytest.mark.parametrize("name", ["source_url", "license_url"])
@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@stock.example/logo",
        "https://user@stock.example/logo",
        "http://:secret@stock.example/",
        "https://stock.example@evil.example/",
    ],
)
def test_a_url_with_credentials_is_refused(name: str, url: str) -> None:
    with pytest.raises(ValueError, match=name):
        alone(name, url)


@pytest.mark.parametrize("name", URL_FIELDS)
@pytest.mark.parametrize("url", BAD_URLS_WITH_A_SECRET)
def test_an_error_about_a_url_never_holds_the_url(name: str, url: str) -> None:
    with pytest.raises(ValueError, match=name) as caught:
        alone(name, url)

    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value.args)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


@pytest.mark.parametrize("name", URL_FIELDS)
@pytest.mark.parametrize(
    "url", ["http://host:abc/", "http://host:99999/", "http://host:-1/"]
)
def test_a_bad_port_is_refused(name: str, url: str) -> None:
    with pytest.raises(ValueError, match=name):
        alone(name, url)


@pytest.mark.parametrize("name", URL_FIELDS)
def test_a_valid_port_is_accepted(name: str) -> None:
    for url in ("http://host:8080/x", "https://host:65535/"):
        assert getattr(alone(name, url), name) == url


@pytest.mark.parametrize("name", URL_FIELDS)
@pytest.mark.parametrize("parameter", LISTED_SECRET_NAMES)
def test_a_url_with_a_secret_looking_parameter_is_refused(
    name: str, parameter: str
) -> None:
    with pytest.raises(ValueError, match=name) as caught:
        alone(name, f"https://stock.example/a?id=1&{parameter}={SECRET}&page=2")

    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("name", URL_FIELDS)
@pytest.mark.parametrize(
    "url",
    [
        "https://stock.example/a?ToKeN=SECRET",
        "https://stock.example/a?Api_Key=SECRET",
        "https://stock.example/a?X-Amz-Signature=SECRET",
        "https://stock.example/a?X-GOOG-Credential=SECRET",
        "https://stock.example/a?%74oken=SECRET",  # percent-encoded name
        "https://stock.example/a?token",  # no value
        "https://stock.example/a?token=",
        "https://stock.example/a#access_token=SECRET",
        "https://stock.example/a#state=1&Token=SECRET",
        "https://stock.example/a#/route?password=SECRET",
        "https://stock.example/a?id=1#sig=SECRET",
    ],
)
def test_a_secret_looking_parameter_is_refused_in_any_case_and_place(
    name: str, url: str
) -> None:
    with pytest.raises(ValueError, match=name) as caught:
        alone(name, url)

    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("name", URL_FIELDS)
@pytest.mark.parametrize(
    "url",
    [
        "https://stock.example/a?id=123&page=2",
        "https://stock.example/a?monkey=1",
        "https://stock.example/a?keyboard=1&mykey=2&key_id=3&tokens=4",
        "https://stock.example/a?x-amazing=1&x-am=2&xamz-a=3",
        "https://stock.example/a?id=token&page=secret",  # a name decides, not a value
        "https://stock.example/a?mode=1#section-2",
        "https://stock.example/a#/route?id=7",
        "https://stock.example/token/key/secret",  # a path is not a parameter
    ],
)
def test_a_harmless_parameter_is_accepted_as_given(name: str, url: str) -> None:
    assert getattr(alone(name, url), name) == url


def test_the_secret_parameter_names_are_the_documented_constants() -> None:
    assert set(LISTED_SECRET_NAMES[:21]) == provenance.SECRET_PARAMETERS
    assert provenance.SECRET_PARAMETER_PREFIXES == ("x-amz-", "x-goog-")


def test_an_at_sign_after_the_host_is_not_userinfo() -> None:
    url = "https://stock.example/@lan/logo?mail=a@b.example"

    assert make(source_url=url).source_url == url


@pytest.mark.parametrize("name", ["source_url", "license_url"])
@pytest.mark.parametrize(
    "url",
    [
        "https://stock.example/a b",
        "https://stock .example/logo",
        "https://stock.example/a\tb",
        "https://stock.example/a\nb",
        "https://stock.example/a b",
    ],
)
def test_whitespace_inside_a_url_is_refused(name: str, url: str) -> None:
    with pytest.raises(ValueError, match=name):
        alone(name, url)


def test_a_url_of_exactly_2048_characters_is_accepted_and_one_more_fails() -> None:
    prefix = "https://stock.example/"
    exact = prefix + "a" * (provenance.MAX_URL_LENGTH - len(prefix))

    assert make(source_url=exact).source_url == exact
    with pytest.raises(ValueError, match="source_url"):
        make(source_url=exact + "a")


def test_the_checksum_is_lowercased_and_stripped() -> None:
    assert make(file_sha256=f"  {SHA.upper()}\n").file_sha256 == SHA


@pytest.mark.parametrize(
    "value",
    ["", "ab" * 31, "ab" * 33, "g" * 64, "ab" * 31 + "a-", "ab 12" + "a" * 59],
)
def test_a_bad_checksum_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="file_sha256"):
        make(file_sha256=value)


def test_a_directly_built_checksum_must_already_be_lowercase() -> None:
    fields = dataclasses.asdict(make())
    fields["recorded_by"] = USER
    fields["file_sha256"] = SHA.upper()

    with pytest.raises(ValueError, match="file_sha256"):
        Provenance(**fields)


def test_the_retrieval_time_may_equal_the_recording_time() -> None:
    assert make(retrieved_at=T0).retrieved_at == T0


def test_a_retrieval_time_after_the_recording_time_is_refused() -> None:
    with pytest.raises(ValueError, match="retrieved_at"):
        make(retrieved_at=T0 + timedelta(microseconds=1))


@pytest.mark.parametrize(
    "moment",
    [datetime(2026, 10, 1), datetime(2026, 10, 1, tzinfo=timezone(timedelta(hours=7)))],
)
def test_a_retrieval_time_must_be_utc(moment: datetime) -> None:
    with pytest.raises(ValueError, match="retrieved_at"):
        make(retrieved_at=moment)


def test_a_retrieval_time_must_be_a_datetime() -> None:
    with pytest.raises(TypeError, match="retrieved_at"):
        make(retrieved_at="2026-10-01")  # type: ignore[arg-type]


def test_the_recording_time_comes_from_the_clock_and_must_be_utc() -> None:
    assert make(clock=lambda: T0 + timedelta(hours=1)).created_at == T0 + timedelta(
        hours=1
    )
    with pytest.raises(ValueError, match="created_at"):
        make(clock=lambda: datetime(2026, 10, 4))


def test_without_a_clock_the_time_is_the_current_utc_time() -> None:
    before = datetime.now(UTC)
    made = Provenance.create("a1", owner="Lan", recorded_by=USER)

    assert before <= made.created_at <= datetime.now(UTC)


def test_a_record_with_no_detail_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        Provenance.create("a1", recorded_by=USER, clock=lambda: T0)


def test_an_empty_text_does_not_count_as_a_detail() -> None:
    with pytest.raises(ValueError, match="owner"):
        make(source_url=None, owner="  ")


@pytest.mark.parametrize("asset_id", ["", "   "])
def test_an_asset_id_is_needed(asset_id: str) -> None:
    with pytest.raises(ValueError, match="asset_id"):
        Provenance.create(asset_id, owner="Lan", recorded_by=USER)


def test_the_actor_must_be_an_actor() -> None:
    with pytest.raises(TypeError, match="recorded_by"):
        Provenance.create("a1", owner="Lan", recorded_by="user")  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["owner", "source_url", "file_sha256"])
def test_a_detail_that_is_not_text_is_a_type_error(name: str) -> None:
    with pytest.raises(TypeError, match=name):
        make(**{name: 5})


def test_a_record_is_frozen() -> None:
    made = make()

    with pytest.raises(dataclasses.FrozenInstanceError):
        made.owner = "other"  # type: ignore[misc]


def test_as_dict_holds_every_field() -> None:
    retrieved = T0 - timedelta(days=1)
    made = make(
        retrieved_at=retrieved, license_name="MIT", file_sha256=SHA, recorded_by=AI
    )

    assert made.as_dict() == {
        "id": made.id,
        "asset_id": "a1",
        "source_url": "https://stock.example/logo",
        "retrieved_at": retrieved.isoformat(),
        "license_name": "MIT",
        "license_url": None,
        "license_ref": None,
        "attribution": None,
        "owner": None,
        "file_sha256": SHA,
        "proof": None,
        "recorded_by": {"kind": "ai", "id": "mock/mock-1"},
        "created_at": T0.isoformat(),
    }


def test_ids_are_unique() -> None:
    assert make().id != make().id


def test_the_content_key_ignores_the_id_the_actor_and_the_time() -> None:
    first = make(recorded_by=USER)
    second = make(recorded_by=AI, clock=lambda: T0 + timedelta(hours=3))

    assert first.id != second.id
    assert first.content_key() == second.content_key()


@pytest.mark.parametrize("name", TEXT_FIELDS)
def test_the_content_key_compares_text_after_nfc_without_case_folding(
    name: str,
) -> None:
    nfc = unicodedata.normalize("NFC", "Nguyễn")
    nfd = unicodedata.normalize("NFD", "Nguyễn")

    assert nfc != nfd
    assert alone(name, nfc).content_key() == alone(name, nfd).content_key()
    assert alone(name, "Lan").content_key() != alone(name, "lan").content_key()


def test_the_content_key_normalises_the_collapsed_text() -> None:
    nfd = unicodedata.normalize("NFD", "Nguyễn  Văn")
    nfc = unicodedata.normalize("NFC", "Nguyễn Văn")

    assert make(owner=nfd).content_key() == make(owner=nfc).content_key()


@pytest.mark.parametrize("name", URL_FIELDS)
def test_the_content_key_compares_a_url_exactly_not_after_nfc(name: str) -> None:
    nfc = unicodedata.normalize("NFC", "https://stock.example/café")
    nfd = unicodedata.normalize("NFD", nfc)

    assert nfc != nfd
    assert alone(name, nfc).content_key() != alone(name, nfd).content_key()
    assert alone(name, nfc).content_key() == alone(name, nfc).content_key()


def test_the_content_key_compares_urls_and_the_time_exactly() -> None:
    assert (
        make(source_url="https://a.example/x").content_key()
        != make(source_url="https://A.example/x").content_key()
    )
    assert (
        make(retrieved_at=T0).content_key()
        != make(retrieved_at=T0 - timedelta(microseconds=1)).content_key()
    )
    assert make(retrieved_at=T0).content_key() == make(retrieved_at=T0).content_key()


def test_the_content_key_has_one_slot_per_detail() -> None:
    assert len(make().content_key()) == len(CONTENT_FIELDS)
