import re
from types import SimpleNamespace

import agate
import pytest
from dbt_common.clients.jinja import CallableMacroGenerator, MacroReturn

from dbt.adapters.risingwave.relation import RisingWaveRelation

from tests.unit.test_materialization_relation_lookup import ADAPTER_MACROS


# The two columns risingwave__get_show_indexes_sql selects. It stops at
# indnkeyatts, so a row never carries the include or distributed by columns.
IN_CATALOG = ("__dbt_index_orders_daily_user_id", "user_id")


def catalog_indexes(*rows):
    return {
        "indexes": agate.Table(
            rows,
            column_names=["name", "column_names"],
            column_types=[agate.Text(), agate.Text()],
        )
    }


def model_config(indexes):
    return SimpleNamespace(
        identifier="orders_daily",
        compiled_code="select user_id, order_id, amount from orders",
        config=SimpleNamespace(
            get=lambda key, default=None: {"indexes": indexes}.get(key, default)
        ),
    )


def orders_daily():
    return RisingWaveRelation.create(
        database="dev",
        schema="analytics",
        identifier="orders_daily",
        type="materialized_view",
    )


def index_changes(relation_results, indexes):
    return orders_daily().get_materialized_view_config_change_collection(
        relation_results, model_config(indexes)
    )


def render_update_indexes(relation, changes):
    def dbt_return(value):
        raise MacroReturn(value)

    def raise_compiler_error(message):
        raise ValueError(message)

    macro_sql = ADAPTER_MACROS.read_text()
    context = {
        "config": {},
        "adapter": SimpleNamespace(
            parse_index=lambda index: SimpleNamespace(columns=index["columns"])
        ),
        "exceptions": SimpleNamespace(raise_compiler_error=raise_compiler_error),
        "log": lambda *args, **kwargs: "",
        "return": dbt_return,
    }
    for sibling in set(re.findall(r"{%-?\s*macro\s+(risingwave__\w+)", macro_sql)):
        context[sibling] = CallableMacroGenerator(
            SimpleNamespace(name=sibling, macro_sql=macro_sql), context
        )
    rendered = context["risingwave__update_indexes_on_materialized_view"](
        relation, changes
    )
    return " ".join(rendered.split())


@pytest.mark.parametrize(
    "index",
    [
        {"columns": ["user_id"], "include": ["amount"]},
        {"columns": ["user_id"], "distributed_by": ["user_id"]},
        {"columns": ["user_id"], "include": ["amount"], "distributed_by": ["user_id"]},
    ],
)
def test_unchanged_index_with_include_or_distributed_by_is_not_a_change(index):
    assert index_changes(catalog_indexes(IN_CATALOG), [index]) is None


def test_changed_key_columns_are_still_reported():
    changes = index_changes(
        catalog_indexes(IN_CATALOG),
        [{"columns": ["order_id"], "include": ["amount"]}],
    )

    assert [change.action.value for change in changes.indexes] == ["drop", "create"]
    assert [change.context.column_names for change in changes.indexes] == [
        ("user_id",),
        ("order_id",),
    ]


def test_added_and_dropped_indexes_are_still_reported():
    added = index_changes(
        catalog_indexes(IN_CATALOG),
        [{"columns": ["user_id"], "include": ["amount"]}, {"columns": ["order_id"]}],
    )

    assert [change.action.value for change in added.indexes] == ["create"]
    assert added.indexes[0].context.column_names == ("order_id",)

    dropped = index_changes(
        catalog_indexes(IN_CATALOG, ("__dbt_index_orders_daily_order_id", "order_id")),
        [{"columns": ["user_id"], "include": ["amount"]}],
    )

    assert [change.action.value for change in dropped.indexes] == ["drop"]
    assert dropped.indexes[0].context.name == "__dbt_index_orders_daily_order_id"


def test_create_sql_for_a_real_change_still_carries_include_and_distributed_by():
    changes = index_changes(
        catalog_indexes(IN_CATALOG),
        [
            {
                "columns": ["order_id"],
                "include": ["amount"],
                "distributed_by": ["order_id"],
            }
        ],
    )

    sql = render_update_indexes(orders_daily(), changes.indexes)

    assert 'drop index if exists "dev"."analytics"."__dbt_index_orders_daily_user_id"' in sql
    assert '"__dbt_index_orders_daily_order_id" on "dev"."analytics"."orders_daily" (order_id)' in sql
    assert "include (amount)" in sql
    assert "distributed by (order_id)" in sql
