from dataclasses import dataclass
from typing import Any, Generic, Iterable, Optional, Sequence, Type, TypeVar

from sqlalchemy import func
from sqlmodel import SQLModel, Session, select  # type: ignore[reportMissingImports]


ModelT = TypeVar("ModelT", bound=SQLModel)


@dataclass(slots=True)
class Page(Generic[ModelT]):
    items: Sequence[ModelT]
    total: int
    limit: int
    offset: int

    @property
    def has_next(self) -> bool:
        return self.offset + self.limit < self.total

    @property
    def has_prev(self) -> bool:
        return self.offset > 0


@dataclass(slots=True)
class Pagination:
    limit: int = 50
    offset: int = 0
    max_limit: int = 500

    def clamp(self) -> "Pagination":
        return Pagination(limit=min(self.limit, self.max_limit), offset=self.offset, max_limit=self.max_limit)


class BaseRepository(Generic[ModelT]):
    model: Type[ModelT]

    def __init__(self, session: Session, model: Type[ModelT]):
        self.session = session
        self.model = model

    def get(self, id_: int) -> Optional[ModelT]:
        return self.session.get(self.model, id_)

    def list(
        self,
        *,
        where=None,
        order_by=None,
        pagination: Optional[Pagination] = None,
    ) -> Page[ModelT]:
        paged = pagination.clamp() if pagination else None
        stmt: Any = select(self.model)
        if where is not None:
            stmt = stmt.where(where)
        if order_by is not None:
            stmt = stmt.order_by(order_by)

        count_stmt: Any = select(func.count()).select_from(self.model)
        if where is not None:
            count_stmt = count_stmt.where(where)
        total = int(self.session.exec(count_stmt).first() or 0)

        if paged:
            stmt = stmt.offset(paged.offset).limit(paged.limit)

        items: Iterable[ModelT] = self.session.exec(stmt).all()
        return Page(
            items=list(items),
            total=total,
            limit=paged.limit if paged else total,
            offset=paged.offset if paged else 0,
        )

    def add(self, obj: ModelT, *, flush: bool = True) -> ModelT:
        self.session.add(obj)
        if flush:
            self.session.flush()
        return obj

    def delete(self, obj: ModelT) -> None:
        self.session.delete(obj)
