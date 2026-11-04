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
            with calculations.request(connection):
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


def test_equal_sqlite_counters_from_other_databases_do_not_reuse_their_rows(tmp_path: Path) -> None:
    table = Table("inputs", MetaData(), Column("value", String))
    calculations = LibraryCalculations()
    for index in range(2):
        engine = create_engine(f"sqlite:///{tmp_path / f'{index}.sqlite3'}")
        table.create(engine)
        with engine.connect() as connection:
            connection.execute(table.insert().values(value=str(index)))
            connection.commit()
            with calculations.request(connection):
                result = reuse_library_calculation(
                    connection, "facts", (select(table),), (), lambda: connection.execute(select(table.c.value)).scalar_one()
                )
                assert result == str(index)
        engine.dispose()


def test_failed_calculation_and_scope_exit_do_not_publish_a_saved_result() -> None:
    engine = create_engine("sqlite://")
    calculate = Mock(side_effect=[RuntimeError("interrupted"), {"facts": []}, {"fresh": True}])
    calculations = LibraryCalculations()
    with engine.connect() as connection:
        with calculations.request(connection):
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
            with calculations.request(connection):
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


def test_calculations_use_one_snapshot_and_next_request_sees_committed_edits(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'inputs.sqlite3'}")
    table = Table("inputs", MetaData(), Column("value", String))
    table.create(engine)
    calculations = LibraryCalculations()
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        connection.execute(table.insert().values(value="first"))
        connection.commit()
        with calculations.request(connection):

            def changing_read() -> str:
                with engine.begin() as concurrent_writer:
                    concurrent_writer.execute(table.update().values(value="changed"))
                return connection.execute(select(table.c.value)).scalar_one()

            assert reuse_library_calculation(connection, "row", (select(table),), (), changing_read) == "first"
        with calculations.request(connection):
            assert (
                reuse_library_calculation(
                    connection, "row", (select(table),), (), lambda: connection.execute(select(table.c.value)).scalar_one()
                )
                == "changed"
            )
        with engine.begin() as writer:
            writer.execute(table.update().values(value="first"))
        with calculations.request(connection):
            assert (
                reuse_library_calculation(
                    connection, "row", (select(table),), (), lambda: connection.execute(select(table.c.value)).scalar_one()
                )
                == "first"
            )
    engine.dispose()


def test_work_progress_does_not_prevent_reusing_unchanged_library_calculations(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'work.sqlite3'}")
    schema = MetaData()
    facts = Table("facts", schema, Column("value", Integer))
    work = Table("work", schema, Column("progress", Integer))
    schema.create_all(engine)
    with engine.begin() as setup:
        setup.exec_driver_sql("PRAGMA journal_mode=WAL")
        setup.execute(facts.insert().values(value=1))
        setup.execute(work.insert().values(progress=0))
    calculations = LibraryCalculations()
    calls = 0
    with engine.connect() as connection:

        def calculate() -> int:
            nonlocal calls
            calls += 1
            with engine.begin() as writer:
                writer.execute(work.update().values(progress=calls))
            return connection.execute(select(facts.c.value)).scalar_one()

        for _ in range(3):
            with calculations.request(connection):
                assert reuse_library_calculation(connection, "facts", (select(facts),), (), calculate) == 1
        assert calls == 1
    engine.dispose()


def test_rollback_after_a_write_inside_the_read_snapshot_does_not_reuse_the_written_row(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback.sqlite3'}")
    table = Table("inputs", MetaData(), Column("value", String))
    table.create(engine)
    calculations = LibraryCalculations()
    with engine.connect() as connection:
        connection.execute(table.insert().values(value="first"))
        connection.commit()

        def read() -> str:
            return reuse_library_calculation(
                connection, "facts", (select(table),), (), lambda: connection.execute(select(table.c.value)).scalar_one()
            )

        with calculations.request(connection):
            assert read() == "first"
            with connection.begin_nested() as transaction:
                connection.execute(table.update().values(value="changed"))
                assert read() == "changed"
                transaction.rollback()
            assert read() == "first"
    engine.dispose()
