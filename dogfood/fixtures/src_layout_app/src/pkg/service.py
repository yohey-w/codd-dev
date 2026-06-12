"""A small service module under a src/ layout with a package directory."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    qty: int


class Catalog:
    """An in-memory catalog keyed by SKU."""

    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def add(self, item: Item) -> None:
        self._items[item.sku] = item

    def get(self, sku: str) -> Item | None:
        return self._items.get(sku)

    def total_units(self) -> int:
        return sum(i.qty for i in self._items.values())
