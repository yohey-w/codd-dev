"""Invoice domain logic for the round-trip fixture (generic, product-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LineItem:
    description: str
    cents: int
    qty: int = 1

    def subtotal(self) -> int:
        return self.cents * self.qty


@dataclass
class Invoice:
    number: str
    lines: list[LineItem] = field(default_factory=list)

    def add_line(self, line: LineItem) -> None:
        self.lines.append(line)

    def total(self) -> int:
        return sum(line.subtotal() for line in self.lines)

    def apply_discount(self, percent: int) -> int:
        gross = self.total()
        return gross - (gross * percent // 100)
