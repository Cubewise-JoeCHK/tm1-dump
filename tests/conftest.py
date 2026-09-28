"""Shared fixtures and test doubles for the tm1-dump test suite."""

from __future__ import annotations

import csv
import io
import re

import pytest
from TM1py.Objects import Chore, Cube, Dimension, Process, Subset, User

from tm1_dump.cli import build_parser


def load_args(zip_path, *extra: str):
    """Parse a ``load`` command line against the fixture zip."""
    return build_parser().parse_args(
        ["load", str(zip_path), "--address", "srv", "--port", "12354", "--user", "admin", "--password", "pw", *extra]
    )


MUTATING_ACTIONS = frozenset(
    {"upsert", "delete", "create_group", "create_user", "update_user", "add_user_to_groups", "write_value", "write_values"}
)

#: Dimension order of the TM1 global-security control cubes on a real server.
#: ``}ChoreSecurity`` lists ``}Groups`` first; the other three list the
#: secured objects first — the engines must not assume one fixed order.
CONTROL_CUBE_DIMENSIONS: dict[str, list[str]] = {
    "}CubeSecurity": ["}Cubes", "}Groups"],
    "}DimensionSecurity": ["}Dimensions", "}Groups"],
    "}ProcessSecurity": ["}Processes", "}Groups"],
    "}ChoreSecurity": ["}Groups", "}Chores"],
}


class FakeTM1:
    """Minimal TM1Service double: records every call, tiny in-memory model.

    ``calls`` holds ``(action, kind, name, detail)`` tuples where ``kind``
    is the zipio object type (``dimensions``, ..., ``security``, ``data``)
    and ``detail`` carries assertion-relevant extras (create/overwrite,
    view class, created-user state, written cell).
    """

    def __init__(self, existing: set[tuple[str, str]] | None = None, fail: dict[tuple[str, str], Exception] | None = None):
        self.existing = existing or set()
        self.fail = fail or {}
        self.calls: list[tuple[str, str, str, str]] = []
        self.connection_kwargs: dict | None = None
        self.cell_writes: list[tuple[str, list[str], dict]] = []
        self.memberships: dict[str, list[str]] = {}

        self.dimensions = _FakeCrud(self, "dimensions")
        self.cubes = _FakeCubes(self)
        self.views = _FakeViews(self)
        self.subsets = _FakeSubsets(self)
        self.processes = _FakeCrud(self, "processes")
        self.chores = _FakeCrud(self, "chores")
        self.security = _FakeSecurity(self)

    @property
    def mutating_calls(self) -> list[tuple[str, str, str, str]]:
        """Every recorded call that would change the target server."""
        return [call for call in self.calls if call[0] in MUTATING_ACTIONS]

    def _record(self, action: str, kind: str, name: str, detail: str = "") -> None:
        self.calls.append((action, kind, name, detail))

    def _fail_if_injected(self, kind: str, name: str) -> None:
        failure = self.fail.get((kind, name))
        if failure is not None:
            raise failure

    def __enter__(self) -> FakeTM1:
        self._record("connect", "", "")
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class _FakeCrud:
    """exists / update_or_create / delete for one object type."""

    def __init__(self, tm1: FakeTM1, kind: str):
        self._tm1 = tm1
        self._kind = kind

    def exists(self, name: str) -> bool:
        self._tm1._record("exists", self._kind, name)
        return (self._kind, name) in self._tm1.existing

    def update_or_create(self, tm1_object) -> None:
        name = tm1_object.name
        self._tm1._fail_if_injected(self._kind, name)
        detail = "overwrite" if (self._kind, name) in self._tm1.existing else "create"
        self._tm1._record("upsert", self._kind, name, detail)

    def delete(self, name: str) -> None:
        self._tm1._fail_if_injected(self._kind, name)
        self._tm1._record("delete", self._kind, name)


class _FakeCubes(_FakeCrud):
    """Cube service plus the rules and cells surfaces."""

    def __init__(self, tm1: FakeTM1):
        super().__init__(tm1, "cubes")

    def update_or_create_rules(self, cube_name: str, rules) -> None:
        self._tm1._record("upsert_rules", "cubes", cube_name, str(rules))

    def get_dimension_names(self, cube_name: str, **_kwargs) -> list[str]:
        """Answer for the rights control cubes, like a real server does."""
        self._tm1._record("get_dimension_names", "cubes", cube_name)
        return list(CONTROL_CUBE_DIMENSIONS.get(cube_name, []))

    @property
    def cells(self) -> _FakeCells:
        return _FakeCells(self._tm1)


class _FakeCells:
    """Cell write surface: value writes for rights, chunk writes for data."""

    def __init__(self, tm1: FakeTM1):
        self._tm1 = tm1

    def write_value(self, value, cube_name: str, element_tuple: tuple[str, ...], dimensions=None) -> None:
        self._tm1._fail_if_injected("data", cube_name)
        self._tm1._record("write_value", "data", cube_name, repr((value, element_tuple)))

    def write_values(self, cube_name: str, cellset_as_dict: dict, dimensions=None) -> str:
        self._tm1._fail_if_injected("data", cube_name)
        self._tm1.cell_writes.append((cube_name, list(dimensions or []), dict(cellset_as_dict)))
        self._tm1._record("write_values", "data", cube_name, f"{len(cellset_as_dict)} cells")


class _FakeViews(_FakeCrud):
    """View service whose exists() matches the public-only contract."""

    def __init__(self, tm1: FakeTM1):
        super().__init__(tm1, "views")

    def exists(self, cube_name: str, view_name: str, private: bool = False) -> bool:
        self._tm1._record("exists", "views", view_name, cube_name)
        return ("views", view_name) in self._tm1.existing

    def update_or_create(self, view, private: bool = False) -> None:
        self._tm1._fail_if_injected("views", view.name)
        detail = type(view).__name__
        self._tm1._record("upsert", "views", view.name, detail)


class _FakeSubsets(_FakeCrud):
    """Subset service recording whether the upserted subset is dynamic."""

    def __init__(self, tm1: FakeTM1):
        super().__init__(tm1, "subsets")

    def exists(self, subset_name: str, dimension_name: str, hierarchy_name: str = None, private: bool = False) -> bool:
        self._tm1._record("exists", "subsets", subset_name, dimension_name)
        return ("subsets", subset_name) in self._tm1.existing

    def update_or_create(self, subset, private: bool = False) -> None:
        self._tm1._fail_if_injected("subsets", subset.name)
        detail = "dynamic" if getattr(subset, "expression", None) else "static"
        self._tm1._record("upsert", "subsets", subset.name, detail)


class _FakeSecurity:
    """Security surface: groups, users, memberships."""

    def __init__(self, tm1: FakeTM1):
        self._tm1 = tm1

    def group_exists(self, group_name: str) -> bool:
        self._tm1._record("group_exists", "security", group_name)
        return ("group", group_name) in self._tm1.existing

    def create_group(self, group_name: str) -> None:
        self._tm1._record("create_group", "security", group_name)

    def user_exists(self, user_name: str) -> bool:
        self._tm1._record("user_exists", "security", user_name)
        return ("user", user_name) in self._tm1.existing

    def create_user(self, user: User) -> None:
        detail = f"enabled={user.enabled};password_set={bool(user.password)}"
        self._tm1._record("create_user", "security", user.name, detail)

    def get_user(self, user_name: str) -> User:
        self._tm1._record("get_user", "security", user_name)
        return User(name=user_name, groups=self._tm1.memberships.get(user_name, []))

    def update_user(self, user: User) -> None:
        self._tm1._record("update_user", "security", user.name, f"friendly={user.friendly_name}")

    def add_user_to_groups(self, user_name: str, groups) -> None:
        self._tm1._record("add_user_to_groups", "security", user_name, ",".join(groups))
        current = self._tm1.memberships.setdefault(user_name, [])
        current.extend(group for group in groups if group not in current)


def install_fake_tm1(monkeypatch, existing: set[tuple[str, str]] | None = None, fail: dict | None = None) -> FakeTM1:
    """Patch ``tm1_dump.load.TM1Service`` so run_load uses one FakeTM1."""
    fake = FakeTM1(existing=existing, fail=fail)

    def factory(**kwargs):
        fake.connection_kwargs = kwargs
        return fake

    monkeypatch.setattr("tm1_dump.load.TM1Service", factory)
    return fake


# --------------------------------------------------------------------------- model-backed double


class ModelTM1:
    """Model-backed TM1Service double serving both real engines end-to-end.

    Reads (the dump engine) are served from an in-memory model of real
    TM1py objects; writes (the load engine) mutate that model the way the
    real REST surface does:

    - cells are stored positionally, keyed by the cube's dimension order
      (the rights control cubes included, so a permission write lands
      where the dump engine would read it back);
    - deleting a cube drops its views and data, deleting a dimension drops
      its subsets (children go with the parent);
    - creating a view that references named subsets that do not exist
      fails, like TM1's REST error on an unresolvable subset bind;
    - public and private views/subsets are separate stores, so the
      public-only contract stays observable.

    Mutating calls are recorded in ``calls`` with the same action
    vocabulary as :class:`FakeTM1`, so ``mutating_calls`` works here too.
    Assertion helpers: :meth:`read_cells` plus the TM1py-shaped service
    getters (``get_all_names``, ``get``, ``get_all``, ``get_groups``...).
    """

    def __init__(self, server_name: str = "Model") -> None:
        self.server_name = server_name
        self.connection_kwargs: dict | None = None
        self.calls: list[tuple[str, str, str, str]] = []
        self.logout_calls = 0

        self._dimensions: dict[str, Dimension] = {}
        self._cubes: dict[str, Cube] = {}
        self._views: dict[str, dict[str, object]] = {}
        self._private_views: dict[str, dict[str, object]] = {}
        self._subsets: dict[tuple[str, str], dict[str, Subset]] = {}
        self._private_subsets: dict[tuple[str, str], dict[str, Subset]] = {}
        self._processes: dict[str, Process] = {}
        self._chores: dict[str, Chore] = {}
        self._groups: list[str] = []
        self._users: dict[str, User] = {}
        self._user_groups: dict[str, list[str]] = {}
        self._data: dict[str, dict[tuple, object]] = {}

        self.dimensions = _ModelDimensions(self)
        self.hierarchies = _ModelHierarchies(self)
        self.cubes = _ModelCubes(self)
        self.views = _ModelViews(self)
        self.subsets = _ModelSubsets(self)
        self.processes = _ModelObjects(self, "processes", self._processes)
        self.chores = _ModelObjects(self, "chores", self._chores)
        self.security = _ModelSecurity(self)
        self.server = _ModelServer(self)
        # the dump engine reads cells at the top level (tm1.cells)
        self.cells = _ModelCells(self)

    # -- assertion helpers ----------------------------------------------------

    def read_cells(self, cube_name: str) -> dict[tuple, object]:
        """Stored cells of one cube, keyed by dimension-ordered element tuples."""
        return dict(self._data.get(cube_name, {}))

    # -- engine plumbing -------------------------------------------------------

    def _record(self, action: str, kind: str, name: str, detail: str = "") -> None:
        self.calls.append((action, kind, name, detail))

    @property
    def mutating_calls(self) -> list[tuple[str, str, str, str]]:
        """Every recorded call that would change the target server."""
        return [call for call in self.calls if call[0] in MUTATING_ACTIONS]

    def __enter__(self) -> ModelTM1:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def logout(self) -> None:
        self.logout_calls += 1


class _ModelDimensions:
    """Dimension reads plus cascade-aware upsert/delete."""

    def __init__(self, model: ModelTM1):
        self._model = model

    def get_all_names(self) -> list[str]:
        return sorted(self._model._dimensions)

    def get(self, name: str) -> Dimension:
        return self._model._dimensions[name]

    def exists(self, name: str) -> bool:
        self._model._record("exists", "dimensions", name)
        return name in self._model._dimensions

    def update_or_create(self, dimension: Dimension) -> None:
        detail = "overwrite" if dimension.name in self._model._dimensions else "create"
        self._model._dimensions[dimension.name] = dimension
        self._model._record("upsert", "dimensions", dimension.name, detail)

    def delete(self, name: str) -> None:
        self._model._dimensions.pop(name, None)
        # a deleted dimension takes its subsets (public and private) with it
        for store in (self._model._subsets, self._model._private_subsets):
            for hierarchy_key in [key for key in store if key[0] == name]:
                del store[hierarchy_key]
        self._model._record("delete", "dimensions", name)


class _ModelHierarchies:
    """Hierarchy names of one dimension, straight from the model."""

    def __init__(self, model: ModelTM1):
        self._model = model

    def get_all_names(self, dimension_name: str) -> list[str]:
        dimension = self._model._dimensions.get(dimension_name)
        return [hierarchy.name for hierarchy in dimension.hierarchies] if dimension else []


class _ModelCubes:
    """Cube service plus rules, dimension names and the cells surface."""

    def __init__(self, model: ModelTM1):
        self._model = model

    def get_all(self) -> list[Cube]:
        return list(self._model._cubes.values())

    def get_dimension_names(self, cube_name: str, **_kwargs) -> list[str]:
        if cube_name in CONTROL_CUBE_DIMENSIONS:
            return list(CONTROL_CUBE_DIMENSIONS[cube_name])
        cube = self._model._cubes.get(cube_name)
        return list(cube.dimensions) if cube else []

    def exists(self, name: str) -> bool:
        self._model._record("exists", "cubes", name)
        return name in self._model._cubes

    def update_or_create(self, cube: Cube) -> None:
        detail = "overwrite" if cube.name in self._model._cubes else "create"
        self._model._cubes[cube.name] = cube
        self._model._record("upsert", "cubes", cube.name, detail)

    def update_or_create_rules(self, cube_name: str, rules) -> None:
        cube = self._model._cubes.get(cube_name)
        if cube is not None:
            cube.rules = rules
        self._model._record("upsert_rules", "cubes", cube_name, str(rules))

    def delete(self, name: str) -> None:
        self._model._cubes.pop(name, None)
        # a deleted cube takes its views and data with it
        self._model._views.pop(name, None)
        self._model._private_views.pop(name, None)
        self._model._data.pop(name, None)
        self._model._record("delete", "cubes", name)

    @property
    def cells(self) -> _ModelCells:
        return _ModelCells(self._model)


class _ModelCells:
    """Cell surface: MDX CSV reads for the dump, positional writes for the load."""

    def __init__(self, model: ModelTM1):
        self._model = model

    def execute_mdx_csv(self, mdx: str, skip_zeros: bool = False, use_blob: bool = False, **_kwargs) -> str:
        """Serve the stored cells of the MDX's cube as TM1-style CSV."""
        cube_name = mdx.rsplit("FROM [", 1)[1].removesuffix("]").replace("]]", "]")
        select_part = mdx.split("SELECT NON EMPTY ", 1)[1].split(" ON 0", 1)[0]
        dimension_names = [
            member.lstrip("{[").removesuffix("].MEMBERS}").replace("]]", "]")
            for member in select_part.split(" * ")
        ]
        rows = []
        for coords, value in sorted(self._model._data.get(cube_name, {}).items()):
            if skip_zeros and (value is None or value == "" or value == 0):
                continue
            rows.append([*coords, str(value)])
        if not rows:
            return ""
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")  # RFC-4180 quoting, like the real server
        writer.writerow([*dimension_names, "Value"])
        writer.writerows(rows)
        return buffer.getvalue().rstrip("\r\n")

    def write_value(self, value, cube_name: str, element_tuple: tuple[str, ...], dimensions=None) -> None:
        coords = tuple(element_tuple)
        self._model._data.setdefault(cube_name, {})[coords] = value
        self._model._record("write_value", "data", cube_name, repr((value, coords)))

    def write_values(self, cube_name: str, cellset_as_dict: dict, dimensions=None) -> str:
        self._model._data.setdefault(cube_name, {}).update(cellset_as_dict)
        self._model._record("write_values", "data", cube_name, f"{len(cellset_as_dict)} cells")


_SUBSET_BIND = re.compile(r"Dimensions\('([^']+)'\)/Hierarchies\('([^']+)'\)/Subsets\('([^']+)'\)")


class _ModelViews:
    """View service with the (private, public) get_all order and subset binds."""

    def __init__(self, model: ModelTM1):
        self._model = model

    def get_all(self, cube_name: str, **_kwargs) -> tuple[list, list]:
        """Mirror tm1py's return order: (private_views, public_views)."""
        private = list(self._model._private_views.get(cube_name, {}).values())
        public = list(self._model._views.get(cube_name, {}).values())
        return private, public

    def exists(self, cube_name: str, view_name: str, private: bool = False) -> bool:
        self._model._record("exists", "views", view_name, cube_name)
        store = self._model._private_views if private else self._model._views
        return view_name in store.get(cube_name, {})

    def update_or_create(self, view, private: bool = False) -> None:
        # TM1 rejects a view whose named subsets do not exist; mirror that so
        # a wrong subset/view load order fails the roundtrip loudly.
        for dimension_name, hierarchy_name, subset_name in _SUBSET_BIND.findall(view.body):
            if subset_name not in self._model._subsets.get((dimension_name, hierarchy_name), {}):
                raise ValueError(
                    f"subset {subset_name!r} not found in "
                    f"{dimension_name}({hierarchy_name}) — TM1 rejects views referencing missing subsets"
                )
        store = self._model._private_views if private else self._model._views
        store.setdefault(view.cube, {})[view.name] = view
        self._model._record("upsert", "views", view.name, type(view).__name__)


class _ModelSubsets:
    """Subset service keyed by (dimension, hierarchy), public and private."""

    def __init__(self, model: ModelTM1):
        self._model = model

    @staticmethod
    def _hierarchy_key(dimension_name: str, hierarchy_name: str | None) -> tuple[str, str]:
        return dimension_name, hierarchy_name or dimension_name

    def get_all_names(self, dimension_name: str, hierarchy_name: str = None, private: bool = False, **_kwargs) -> list[str]:
        store = self._model._private_subsets if private else self._model._subsets
        return list(store.get(self._hierarchy_key(dimension_name, hierarchy_name), {}))

    def get(self, subset_name: str, dimension_name: str, hierarchy_name: str = None, **_kwargs) -> Subset:
        return self._model._subsets[self._hierarchy_key(dimension_name, hierarchy_name)][subset_name]

    def exists(self, subset_name: str, dimension_name: str, hierarchy_name: str = None, private: bool = False, **_kwargs) -> bool:
        self._model._record("exists", "subsets", subset_name, dimension_name)
        store = self._model._private_subsets if private else self._model._subsets
        return subset_name in store.get(self._hierarchy_key(dimension_name, hierarchy_name), {})

    def update_or_create(self, subset, private: bool = False) -> None:
        store = self._model._private_subsets if private else self._model._subsets
        key = self._hierarchy_key(subset.dimension_name, subset.hierarchy_name)
        detail = "dynamic" if getattr(subset, "expression", None) else "static"
        store.setdefault(key, {})[subset.name] = subset
        self._model._record("upsert", "subsets", subset.name, detail)


class _ModelObjects:
    """Plain exists/update_or_create/delete dict service (processes, chores)."""

    def __init__(self, model: ModelTM1, kind: str, store: dict):
        self._model = model
        self._kind = kind
        self._store = store

    def get_all(self) -> list:
        return list(self._store.values())

    def exists(self, name: str) -> bool:
        self._model._record("exists", self._kind, name)
        return name in self._store

    def update_or_create(self, tm1_object) -> None:
        detail = "overwrite" if tm1_object.name in self._store else "create"
        self._store[tm1_object.name] = tm1_object
        self._model._record("upsert", self._kind, tm1_object.name, detail)

    def delete(self, name: str) -> None:
        self._store.pop(name, None)
        self._model._record("delete", self._kind, name)


class _ModelSecurity:
    """Security surface: groups, users, memberships — nothing ever deleted."""

    def __init__(self, model: ModelTM1):
        self._model = model

    def get_all_users(self) -> list[User]:
        return list(self._model._users.values())

    def get_all_groups(self) -> list[str]:
        return list(self._model._groups)

    def get_groups(self, user_name: str) -> list[str]:
        return list(self._model._user_groups.get(user_name, []))

    def group_exists(self, group_name: str) -> bool:
        self._model._record("group_exists", "security", group_name)
        return group_name in self._model._groups

    def create_group(self, group_name: str) -> None:
        self._model._groups.append(group_name)
        self._model._record("create_group", "security", group_name)

    def user_exists(self, user_name: str) -> bool:
        self._model._record("user_exists", "security", user_name)
        return user_name in self._model._users

    def create_user(self, user: User) -> None:
        detail = f"enabled={user.enabled};password_set={bool(user.password)}"
        self._model._users[user.name] = user
        self._model._user_groups[user.name] = list(user.groups or [])
        self._model._record("create_user", "security", user.name, detail)

    def get_user(self, user_name: str) -> User:
        self._model._record("get_user", "security", user_name)
        return self._model._users[user_name]

    def update_user(self, user: User) -> None:
        self._model._users[user.name] = user
        self._model._record("update_user", "security", user.name, f"friendly={user.friendly_name}")

    def add_user_to_groups(self, user_name: str, groups) -> None:
        self._model._record("add_user_to_groups", "security", user_name, ",".join(groups))
        current = self._model._user_groups.setdefault(user_name, [])
        current.extend(group for group in groups if group not in current)


class _ModelServer:
    def __init__(self, model: ModelTM1):
        self._model = model

    def get_server_name(self) -> str:
        return self._model.server_name

    def get_product_version(self) -> str:
        return "11.8.00000.27"


def install_model_tm1(monkeypatch, source: ModelTM1 | None = None, target: ModelTM1 | None = None) -> tuple[ModelTM1, ModelTM1]:
    """Wire both real engines onto a model-backed source/target pair.

    Patches ``tm1_dump.dump._connect`` to return ``source`` and
    ``tm1_dump.load.TM1Service`` to return ``target``.
    """
    source = source if source is not None else ModelTM1(server_name="Source")
    target = target if target is not None else ModelTM1(server_name="Target")

    monkeypatch.setattr("tm1_dump.dump._connect", lambda conn: source)

    def factory(**kwargs):
        target.connection_kwargs = kwargs
        return target

    monkeypatch.setattr("tm1_dump.load.TM1Service", factory)
    return source, target


@pytest.fixture(name="install_fake_tm1")
def fixture_install_fake_tm1(monkeypatch):
    """Fixture wrapper around :func:`install_fake_tm1`."""

    def _install(existing=None, fail=None) -> FakeTM1:
        return install_fake_tm1(monkeypatch, existing=existing, fail=fail)

    return _install


@pytest.fixture(name="fixture_zip_path")
def fixture_fixture_zip_path(tmp_path) -> str:
    """A fresh contract-valid fixture dump zip."""
    from fixture_dump import write_fixture_zip

    zip_path = tmp_path / "basic_dump.zip"
    write_fixture_zip(zip_path)
    return str(zip_path)
