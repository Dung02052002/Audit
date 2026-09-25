import threading
from typing import Protocol

import pytest

from ai_youtube_agent.core.di import Container, Lifetime, RegistrationError
from ai_youtube_agent.core.errors import ApplicationError


class Clock(Protocol):
    def now(self) -> str: ...


class FixedClock:
    def now(self) -> str:
        return "fixed"


class OtherClock:
    def now(self) -> str:
        return "other"


class Repository:
    pass


class Service:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository


def test_resolves_registered_implementation() -> None:
    container = Container()
    container.register(Clock, lambda _: FixedClock())

    assert container.resolve(Clock).now() == "fixed"


def test_singleton_is_built_once() -> None:
    container = Container()
    container.register(Repository, lambda _: Repository())

    assert container.resolve(Repository) is container.resolve(Repository)


def test_transient_is_built_every_time() -> None:
    container = Container()
    container.register(Repository, lambda _: Repository(), lifetime=Lifetime.TRANSIENT)

    assert container.resolve(Repository) is not container.resolve(Repository)


def test_factory_resolves_its_own_dependencies() -> None:
    container = Container()
    container.register(Repository, lambda _: Repository())
    container.register(Service, lambda c: Service(c.resolve(Repository)))

    assert container.resolve(Service).repository is container.resolve(Repository)


def test_register_instance() -> None:
    container = Container()
    repository = Repository()
    container.register_instance(Repository, repository)

    assert container.resolve(Repository) is repository


def test_missing_registration_is_an_application_error() -> None:
    with pytest.raises(RegistrationError, match="Repository is not registered") as err:
        Container().resolve(Repository)

    assert isinstance(err.value, ApplicationError)
    assert err.value.code == "application.dependency_injection"


def test_duplicate_registration_is_rejected() -> None:
    container = Container()
    container.register(Repository, lambda _: Repository())

    with pytest.raises(RegistrationError, match="already registered"):
        container.register(Repository, lambda _: Repository())


def test_wrong_type_from_factory_is_rejected() -> None:
    container = Container()
    container.register(Repository, lambda _: "not a repository")  # type: ignore[arg-type,return-value]

    with pytest.raises(RegistrationError, match="returned str"):
        container.resolve(Repository)


def test_circular_dependency_is_detected() -> None:
    class A:
        pass

    class B:
        pass

    container = Container()
    container.register(A, lambda c: (c.resolve(B), A())[1])
    container.register(B, lambda c: (c.resolve(A), B())[1])

    with pytest.raises(
        RegistrationError, match="circular dependency: .*A -> .*B -> .*A"
    ):
        container.resolve(A)


def test_override_swaps_and_restores() -> None:
    container = Container()
    container.register(Clock, lambda _: FixedClock())
    original = container.resolve(Clock)

    with container.override(Clock, lambda _: OtherClock()):
        assert container.resolve(Clock).now() == "other"

    assert container.resolve(Clock) is original


def test_override_requires_existing_registration() -> None:
    with (
        pytest.raises(RegistrationError, match="not registered"),
        Container().override(Clock, lambda _: FixedClock()),
    ):
        pass


def test_is_registered() -> None:
    container = Container()
    container.register(Repository, lambda _: Repository())

    assert container.is_registered(Repository)
    assert not container.is_registered(Service)


def test_singleton_is_built_once_across_threads() -> None:
    container = Container()
    built: list[Repository] = []

    def factory(_: Container) -> Repository:
        repository = Repository()
        built.append(repository)
        return repository

    container.register(Repository, factory)
    results: list[Repository] = []
    threads = [
        threading.Thread(target=lambda: results.append(container.resolve(Repository)))
        for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(built) == 1
    assert all(result is built[0] for result in results)
