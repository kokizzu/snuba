from abc import ABC, abstractmethod, abstractproperty
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Sequence, Set, TypeVar, Union

from snuba import settings


class Category(Enum):
    MIGRATIONS = "migrations"


class Resource(ABC):
    def __init__(self, name: str) -> None:
        self.name = name

    @abstractproperty
    def category(self) -> Category:
        raise NotImplementedError


class MigrationResource(Resource):
    def category(self) -> Category:
        return Category.MIGRATIONS


TResource = TypeVar("TResource", bound=Resource)


class Action(ABC, Generic[TResource]):
    """
    An action is used to describe the permissions a user has
    on a specific set of resources.
    """

    @abstractmethod
    def validated_resources(
        self, resources: Sequence[TResource]
    ) -> Sequence[TResource]:
        """
        A resource is considered valid if the action can be
        taken on the resource.

        e.g. a user can "execute" (action) a migration within
        a "migration group" (resource)

        Raise an error if any resources are invalid, otherwise
        return the resources.
        """
        raise NotImplementedError


class MigrationAction(Action[MigrationResource]):
    def __init__(self, resources: Sequence[MigrationResource]) -> None:
        self._resources = self.validated_resources(resources)

    def validated_resources(
        self, resources: Sequence[MigrationResource]
    ) -> Sequence[MigrationResource]:
        return resources


class ExecuteAllAction(MigrationAction):
    pass


class ExecuteNonBlockingAction(MigrationAction):
    pass


class ExecuteNoneAction(MigrationAction):
    pass


# todo, shoudln't need .keys() once ADMIN_ALLOWED_MIGRATION_GROUPS is a set not dict
MIGRATIONS_RESOURCES = {
    group: MigrationResource(group)
    for group in settings.ADMIN_ALLOWED_MIGRATION_GROUPS.keys()
}


@dataclass(frozen=True)
class Role:
    name: str
    actions: Set[Union[MigrationAction]]


DEFAULT_ROLES = [
    Role(
        name="MigrationsReader",
        actions={ExecuteNoneAction(list(MIGRATIONS_RESOURCES.values()))},
    ),
    Role(
        name="MigrationsLimitedExecutor",
        actions={ExecuteNonBlockingAction(list(MIGRATIONS_RESOURCES.values()))},
    ),
    Role(
        name="TestMigrationsExecutor",
        actions={ExecuteAllAction([MIGRATIONS_RESOURCES["test_migration"]])},
    ),
]