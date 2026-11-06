from __future__ import annotations

from typing import Any

from exceptions import BackendContractError


def _field(data: dict[str, Any], keys: tuple[str, ...], kind: type, minimum: int = 0) -> Any:
    values = [data[key] for key in keys if key in data]
    if not values:
        return None
    if any(type(value) is not kind or (kind is int and value < minimum) for value in values):
        raise BackendContractError(f"Response pagination {keys[0]} has an invalid type or range.")
    if any(value != values[0] for value in values[1:]):
        raise BackendContractError(f"Response pagination aliases for {keys[0]} disagree.")
    return values[0]


def read_pagination(
    data: dict[str, Any], *, requested_page: int | None = None, requested_size: int | None = None,
) -> dict[str, int | bool | None]:
    """Validate optional wire metadata without inventing missing counts or page numbers."""
    if not isinstance(data, dict):
        raise BackendContractError("Response pagination must belong to an object.")
    page = _field(data, ("page", "number", "pageNumber"), int)
    size = _field(data, ("size", "pageSize"), int, 1)
    total_elements = _field(data, ("total_elements", "totalElements"), int)
    total_pages = _field(data, ("total_pages", "totalPages"), int)
    has_next = _field(data, ("has_next", "hasNext"), bool)
    is_last = _field(data, ("is_last", "isLast", "last"), bool)
    if page is not None and requested_page is not None and page != requested_page:
        raise BackendContractError("Response page does not match the requested page.")
    if size is not None and requested_size is not None and size != requested_size:
        raise BackendContractError("Response size does not match the requested size.")
    if total_elements is not None and total_pages is not None and size is not None:
        if total_pages != (total_elements + size - 1) // size:
            raise BackendContractError("Response pagination totals are inconsistent.")
    if has_next is not None and is_last is not None and has_next == is_last:
        raise BackendContractError("Response last and has_next disagree.")
    if page is not None and total_pages is not None:
        expected_next = page + 1 < total_pages
        if has_next is not None and has_next != expected_next:
            raise BackendContractError("Response has_next disagrees with page totals.")
        if is_last is not None and is_last == expected_next:
            raise BackendContractError("Response last disagrees with page totals.")
    if has_next is None and is_last is not None:
        has_next = not is_last
    elif is_last is None and has_next is not None:
        is_last = not has_next
    return {
        "page": page,
        "size": size,
        "total_elements": total_elements,
        "total_pages": total_pages,
        "has_next": has_next,
        "is_last": is_last,
    }
