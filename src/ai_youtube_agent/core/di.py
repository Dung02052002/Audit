"""Dependency injection container (Prompt Pack v8, prompt #009).

Code asks the container for an interface and never names an implementation.
The only place that chooses implementations is the composition root,
``ai_youtube_agent.bootstrap``.

- ``register`` binds an interface to a factory. The factory receives the
  container, so it can resolve its own dependencies.
- ``Lifetime.SINGLETON`` builds the instance once. ``Lifetime.TRANSIENT``
  builds a new one on every ``resolve``.
- ``override`` swaps a binding for the length of a ``with`` block, for tests.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar, cast

from ai_youtube_agent.core.errors import ApplicationError

T = TypeVar("T")
Factory = Callable[["Container"], T]


class Lifetime(StrEnum):
    SINGLETON = "singleton"
    TRANSIENT = "transient"


class RegistrationError(ApplicationError):
    default_code = "application.dependency_injection"


@dataclass
class _Binding:
    factory: Factory[Any]
    lifetime: Lifetime


class Container:
    def __init__(self) -> None:
        self._bindings: dict[type, _Binding] = {}
        self._instances: dict[type, Any] = {}
        self._lock = threading.RLock()
        self._resolving = threading.local()

    def register(
        self,
        interface: type[T],
        factory: Factory[T],
        *,
        lifetime: Lifetime = Lifetime.SINGLETON,
    ) -> None:
        with self._lock:
            if interface in self._bindings:
                raise RegistrationError(f"{_name(interface)} is already registered")
            self._bindings[interface] = _Binding(factory, lifetime)

    def register_instance(self, interface: type[T], instance: T) -> None:
        self.register(interface, lambda _: instance)

    def is_registered(self, interface: type) -> bool:
        return interface in self._bindings

    def resolve(self, interface: type[T]) -> T:
        with self._lock:
            binding = self._bindings.get(interface)
            if binding is None:
                raise RegistrationError(f"{_name(interface)} is not registered")
            if binding.lifetime is Lifetime.SINGLETON and interface in self._instances:
                return cast(T, self._instances[interface])

            stack: list[type] = self._stack()
            if interface in stack:
                cycle = " -> ".join(_name(t) for t in [*stack, interface])
                raise RegistrationError(f"circular dependency: {cycle}")
            stack.append(interface)
            try:
                instance = binding.factory(self)
            finally:
                stack.pop()

            _check_type(interface, instance)
            if binding.lifetime is Lifetime.SINGLETON:
                self._instances[interface] = instance
            return cast(T, instance)

    @contextmanager
    def override(
        self,
        interface: type[T],
        factory: Factory[T],
        *,
        lifetime: Lifetime = Lifetime.SINGLETON,
    ) -> Iterator[None]:
        with self._lock:
            if interface not in self._bindings:
                raise RegistrationError(f"{_name(interface)} is not registered")
            saved = (self._bindings[interface], self._instances.pop(interface, None))
            self._bindings[interface] = _Binding(factory, lifetime)
        try:
            yield
        finally:
            with self._lock:
                self._bindings[interface] = saved[0]
                self._instances.pop(interface, None)
                if saved[1] is not None:
                    self._instances[interface] = saved[1]

    def _stack(self) -> list[type]:
        if not hasattr(self._resolving, "stack"):
            self._resolving.stack = []
        return self._resolving.stack


def _name(interface: type) -> str:
    return getattr(interface, "__qualname__", repr(interface))


def _check_type(interface: type, instance: object) -> None:
    try:
        matches = isinstance(instance, interface)
    except TypeError:
        # Protocols that are not runtime-checkable cannot be checked.
        return
    if not matches:
        raise RegistrationError(
            f"factory for {_name(interface)} returned {type(instance).__qualname__}"
        )
