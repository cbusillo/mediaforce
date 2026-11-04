from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select

from mediaforce.library.calculation_reuse import LibraryCalculations, reuse_library_calculation


def test_saved_inputs_invalidate_for_edits_rollback_and_another_connection(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'inputs.sqlite3'}")
    table = Table("inputs", MetaData(), Column("id", Integer, primary_key=True), Column("value", String))
    table.create(engine)
    calculations = LibraryCalculations()
    calculate = Mock(side_effect=lambda: ["calculated"])
    with engine.connect() as connection:
        connection.execute(table.insert().values(id=1, value="first"))
        connection.commit()

        def read() -> list[str]:
            with calculations.request():
                return reuse_library_calculation(connection, "facts", (select(table),), (), calculate)

        result = read()
        result.append("caller mutation")
        assert read() == ["calculated"]
        calculate.assert_called_once()

        def recomputes() -> None:
            calculate.reset_mock()
            read()
            calculate.assert_called_once()

        connection.execute(table.update().values(value="edited"))
        recomputes()
        connection.rollback()
        recomputes()
        connection.execute(table.insert().values(id=2, value="second"))
        recomputes()
        connection.execute(table.delete().where(table.c.id == 1))
        recomputes()
        connection.commit()
        calculate.reset_mock()
        read()
        calculate.assert_not_called()
        with engine.begin() as writer:
            writer.execute(table.update().values(value="external edit"))
        recomputes()
    engine.dispose()


def test_other_inputs_and_database_identity_are_not_confused(tmp_path: Path) -> None:
    table = Table("inputs", MetaData(), Column("value", String))
    calculations = LibraryCalculations()
    calculate = Mock(side_effect=lambda: {"facts": []})
    for index in range(2):
        engine = create_engine(f"sqlite:///{tmp_path / f'{index}.sqlite3'}")
        table.create(engine)
        with engine.begin() as connection:
            connection.execute(table.insert().values(value=str(index)))
            for setting in ("first", "first", "changed"):
                with calculations.request():
                    reuse_library_calculation(connection, "facts", (select(table),), setting, calculate)
        engine.dispose()
    assert calculate.call_count == 4


def test_failed_calculation_and_scope_exit_do_not_publish_a_saved_result() -> None:
    engine = create_engine("sqlite://")
    calculate = Mock(side_effect=[RuntimeError("interrupted"), {"facts": []}, {"fresh": True}])
    calculations = LibraryCalculations()
    with engine.connect() as connection:
        with calculations.request():
            with pytest.raises(RuntimeError, match="interrupted"):
                reuse_library_calculation(connection, "facts", (), (), calculate)
            assert reuse_library_calculation(connection, "facts", (), (), calculate) == {"facts": []}
        assert reuse_library_calculation(connection, "facts", (), (), calculate) == {"fresh": True}
    engine.dispose()


def test_query_parameters_and_transaction_rollback_do_not_reuse_other_rows(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'inputs.sqlite3'}")
    table = Table("inputs", MetaData(), Column("id", Integer, primary_key=True), Column("value", String))
    table.create(engine)
    calculations = LibraryCalculations()
    with engine.connect() as connection:
        connection.execute(table.insert(), [{"id": 1, "value": "first"}, {"id": 2, "value": "second"}])
        connection.commit()

        def read(item_id: int) -> str:
            query = select(table.c.value).where(table.c.id == item_id)
            with calculations.request():
                return reuse_library_calculation(
                    connection, "row", (query,), item_id, lambda: connection.execute(query).scalar_one()
                )

        assert read(1) == "first"
        assert read(2) == "second"
        with connection.begin_nested() as transaction:
            connection.execute(table.update().where(table.c.id == 2).values(value="changed"))
            assert read(2) == "changed"
            transaction.rollback()
        assert read(2) == "second"
    engine.dispose()


def test_change_during_calculation_is_not_reused_for_older_inputs(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'inputs.sqlite3'}")
    table = Table("inputs", MetaData(), Column("value", String))
    table.create(engine)
    calculations = LibraryCalculations()
    with engine.connect() as connection:
        connection.execute(table.insert().values(value="first"))
        connection.commit()
        with calculations.request():

            def changing_read() -> str:
                with engine.begin() as concurrent_writer:
                    concurrent_writer.execute(table.update().values(value="changed"))
                return connection.execute(select(table.c.value)).scalar_one()

            assert reuse_library_calculation(connection, "row", (select(table),), (), changing_read) == "changed"
        with engine.begin() as writer:
            writer.execute(table.update().values(value="first"))
        with calculations.request():
            assert (
                reuse_library_calculation(
                    connection, "row", (select(table),), (), lambda: connection.execute(select(table.c.value)).scalar_one()
                )
                == "first"
            )
    engine.dispose()
