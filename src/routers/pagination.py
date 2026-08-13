"""The paginated-list envelope, declared once.

Five list endpoints returned the same four fields around a differently-named
array — {contacts,deals,companies,logs,sessions} + total/limit/offset/has_more
— and each recomputed the last one at its return statement:

    has_more=(offset + limit) < total

That expression is easy to get subtly wrong (`<=` yields a phantom extra page
that renders empty) and there was no single place to fix it if it were. Here it
is computed once, in `of()`.

THE WIRE FORMAT IS UNCHANGED. Each subclass keeps its own array field name,
because those names are the API contract the UI reads; this base contributes
only the four fields every envelope already had, in the same order.

    class ContactListResponse(Page):
        contacts: List[ContactSummaryResponse]

    return ContactListResponse.of(rows, total=total, limit=limit, offset=offset)
"""
from pydantic import BaseModel


class Page(BaseModel):
    """The four fields every paginated response carries."""

    total: int
    limit: int
    offset: int
    has_more: bool

    @classmethod
    def of(cls, items, *, total: int, limit: int, offset: int):
        """Fill the envelope, deriving has_more.

        The array field is found rather than named: it is whichever field the
        SUBCLASS declares that this base does not. That keeps each subclass a
        plain one-line declaration — no second place to state which field holds
        the rows, which is the kind of duplication this class exists to remove.
        """
        own = [n for n in cls.model_fields if n not in Page.model_fields]
        if len(own) != 1:
            raise TypeError(
                f"{cls.__name__} must declare exactly one field beyond Page's "
                f"(the array of rows); found {own or 'none'}"
            )
        return cls(
            **{own[0]: items},
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + limit) < total,
        )
