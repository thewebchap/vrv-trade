"""Exchange-linked pricing formulas: recognised, decomposed and compared, never priced.

TradePilot does not fetch exchange prices (LME, COMEX/CME, SHFE, ICE, LBMA...). A contract
price such as "LME of CU basis CSP of a particular day X 90% X CU content (80% recovery)" is a
legitimate pricing mechanism: it is accepted with a note that the market benchmark was not
independently verified. What CAN be checked from the documents is checked: when an invoice
restates the formula, its exchange, metal, benchmark, percentages and premiums must agree
with the contract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EXCHANGES = [("LME", r"\bL?LME\b|LONDON\s+METAL\s+EXCHANGE"), ("COMEX", r"\bCOMEX\b|\bCME\b"),
             ("SHFE", r"\bSHFE\b|SHANGHAI\s+FUTURES"), ("ICE", r"\bICE\b"), ("LBMA", r"\bLBMA\b")]
METALS = [("COPPER", r"\bCU\b|COPPER"), ("ALUMINIUM", r"\bAL\b|ALUMIN(I)?UM"), ("LEAD", r"\bPB\b|\bLEAD\b"),
          ("ZINC", r"\bZN\b|\bZINC\b"), ("NICKEL", r"\bNI\b|NICKEL"), ("TIN", r"\bSN\b|\bTIN\b")]
BENCHMARKS = [("CASH SETTLEMENT", r"\bCSP\b|CASH\s+SETTLEMENT|OFFICIAL\s+CASH"),
              ("MONTHLY AVERAGE", r"MONTHLY\s+AVERAGE|\bMA\b|AVERAGE\s+OF"), ("3-MONTH", r"\b3\s*M\b|THREE\s+MONTHS?"),
              ("SETTLEMENT", r"SETTLEMENT")]
VAGUE = ("CONTENT", "RECOVERY", "ASSAY")


@dataclass
class Formula:
    text: str
    exchange: str | None
    metals: set[str] = field(default_factory=set)
    benchmark: str | None = None
    factors: list[float] = field(default_factory=list)        # commercial percentages (e.g. 90%)
    recovery: list[float] = field(default_factory=list)       # percentages describing content/recovery
    premiums: list[str] = field(default_factory=list)         # "+ USD 50", "less USD 20"

    @property
    def is_exchange(self) -> bool:
        return self.exchange is not None

    def describe(self) -> str:
        parts = [self.exchange or "?"]
        if self.metals:
            parts.append("/".join(sorted(self.metals)))
        if self.benchmark:
            parts.append(self.benchmark)
        parts += [f"x {f:g}%" for f in self.factors] + [f"x {r:g}% recovery/content" for r in self.recovery]
        parts += self.premiums
        return " ".join(parts)


def _first(rules, text):
    for name, rx in rules:
        if re.search(rx, text):
            return name
    return None


def parse_formula(text: str | None) -> Formula | None:
    if not text:
        return None
    u = re.sub(r"\s+", " ", text.upper())
    exchange = _first(EXCHANGES, u)
    if exchange is None:
        return None
    f = Formula(text=text, exchange=exchange,
                metals={name for name, rx in METALS if re.search(rx, u)},
                benchmark=_first(BENCHMARKS, u))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", u):
        after = u[m.end():m.end() + 12]
        open_paren = u.rfind("(", 0, m.start())
        in_paren = open_paren > u.rfind(")", 0, m.start()) and open_paren != -1
        paren_text = u[open_paren: u.find(")", m.end()) + 1] if in_paren else ""
        is_recovery = re.match(r"\s*(RECOVERY|CONTENT|ASSAY)", after) or any(w in paren_text for w in VAGUE)
        (f.recovery if is_recovery else f.factors).append(float(m.group(1)))
    for m in re.finditer(r"(PLUS|\+|PREMIUM|MINUS|LESS|DISCOUNT)\s*(OF\s*)?(USD|US\$|\$)\s*([\d.,]+)", u):
        sign = "+" if m.group(1) in ("PLUS", "+", "PREMIUM") else "-"
        f.premiums.append(f"{sign} USD {m.group(4)}")
    return f


def split_formulas(text: str | None) -> list[str]:
    """'1) LLME of Copper ... 2) LLME of Copper ...' -> one string per numbered formula."""
    if not text:
        return []
    parts = re.split(r"(?:^|\s)\d\)\s*|\s&\s|;\s*(?=L?LME|COMEX|SHFE)", text)
    parts = [p.strip() for p in parts if parse_formula(p)]
    return parts or ([text] if parse_formula(text) else [])


def compare(contract: Formula, invoice: Formula) -> list[str]:
    """Differences between explicitly stated formula elements (empty list = consistent).
    Only elements present on BOTH sides are compared."""
    diffs = []
    if contract.exchange != invoice.exchange:
        diffs.append(f"exchange {contract.exchange} vs {invoice.exchange}")
    if contract.metals and invoice.metals and not (invoice.metals <= contract.metals):
        diffs.append(f"metal {sorted(contract.metals)} vs {sorted(invoice.metals)}")
    if contract.benchmark and invoice.benchmark and contract.benchmark != invoice.benchmark:
        diffs.append(f"benchmark {contract.benchmark} vs {invoice.benchmark}")
    if contract.factors and invoice.factors and sorted(contract.factors) != sorted(invoice.factors):
        diffs.append(f"pricing factor {contract.factors} vs {invoice.factors}")
    if contract.recovery and invoice.recovery and sorted(contract.recovery) != sorted(invoice.recovery):
        diffs.append(f"recovery/content factor {contract.recovery} vs {invoice.recovery}")
    if contract.premiums and invoice.premiums and sorted(contract.premiums) != sorted(invoice.premiums):
        diffs.append(f"premium/discount {contract.premiums} vs {invoice.premiums}")
    return diffs


def invoice_formula(text: str) -> Formula | None:
    """A formula restated on an invoice (lines naming an exchange), if any."""
    lines = [l for l in text.splitlines() if _first(EXCHANGES, l.upper())]
    return parse_formula(" ".join(lines)) if lines else None
