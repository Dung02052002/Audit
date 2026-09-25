# Dashboard

Home of the Command Center (context X3 in `docs/ARCHITECTURE.md`, prompts #191–#203).

This folder is empty on purpose. The requirements call for an "iOS-style" design system (#191), but they do not say whether the Command Center is a native iOS app or a web dashboard. That is open question Q4 in `docs/ARCHITECTURE.md`. The folder sits outside the Python package so that the answer to Q4 is not decided by the folder layout.

The dashboard reads from the backend HTTP API and sends only user actions, such as approve, reject, request changes and settings. It owns no domain rules.
