"""The board's custom design rules (.kicad_dru), as far as routing needs them:
the clearance between two items.

KiCad's Python API loads these rules but cannot be asked for the clearance
between two given items, so the subset a router meets is read here:

  (rule "name" (condition "<expression>") (constraint clearance (min 8mm)))

Expressions: A./B. properties NetClass, NetName, Type, Reference; the
functions memberOfFootprint('R5*'), intersectsCourtyard('U1'),
hasNetclass('HV'); == and != (with * and ? wildcards on strings), &&, ||,
!, parentheses. A rule using anything else is left out and reported.

Precedence is KiCad's: a later rule wins over an earlier one, both orders
of the pair (A, B) and (B, A) are tried, and a pair no rule matches keeps
the larger of the two net-class clearances. The board's minimum clearance
stays the floor.

A rule with (layer outer), (layer inner) or (layer "<name>") holds on those
copper layers only: rules are tried last to first and the first whose
condition and layer both match wins, so an inner 1.0 mm / outer 1.5 mm pair
asks 1.0 mm on In1.Cu and 1.5 mm on F.Cu. between() takes the layer's
canonical name ("F.Cu", "In2.Cu"); without one it answers the strictest of
the layers the rules tell apart (a via, which spans them all).
"""
import fnmatch
import functools
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

MM = 1_000_000


@dataclass(frozen=True)
class Item:
    """What a rule condition can see of one item."""
    netclass: str = "Default"
    netname: str = ""
    ref: str = ""            # the footprint a pad belongs to; "" for tracks and vias
    kind: str = "Track"      # Pad, Track, Via


class Unsupported(Exception):
    pass


# ---- s-expressions -------------------------------------------------------------

def _strip_comments(text: str) -> str:
    """KiCad's rule files take a line whose first non-blank is '#' as a
    comment. Left in, a comment such as "Net-(<ref>-*" opens a list that never
    closes and every rule after it disappears into it."""
    return re.sub(r"(?m)^[ \t]*#.*$", "", text)


def _sexpr(text: str):
    text = _strip_comments(text)
    tokens = re.findall(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()]+', text)
    stack: List[list] = [[]]
    for tok in tokens:
        if tok == "(":
            stack.append([])
        elif tok == ")":
            done = stack.pop()
            stack[-1].append(done)
        elif tok.startswith('"'):
            stack[-1].append(tok[1:-1].replace('\\"', '"'))
        else:
            stack[-1].append(tok)
    return stack[0]


def _length(token: str) -> int:
    m = re.fullmatch(r"(-?[\d.]+)\s*(mm|mil|in)?", token)
    if not m:
        raise Unsupported(f"length {token}")
    v = float(m.group(1))
    unit = m.group(2) or "mm"
    return int(round(v * {"mm": MM, "mil": 25_400, "in": 25_400_000}[unit]))


# ---- condition expressions -----------------------------------------------------

_TOKEN = re.compile(r"\s*(\|\||&&|==|!=|!|\(|\)|'(?:[^'\\]|\\.)*'|[AB]\.\w+|\w+|,)")


def _tokens(expr: str) -> List[str]:
    out, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise Unsupported(f"cannot read '{expr[pos:pos + 20]}'")
        out.append(m.group(1))
        pos = m.end()
        while pos < len(expr) and expr[pos].isspace():
            pos += 1
    return out


@functools.lru_cache(maxsize=None)
def _wild(value: str, pattern: str) -> bool:
    # memoised: a board's rule pairs ask the same (net, pattern) thousands of
    # times (44 wildcard rules x 380 nets took minutes per pair table)
    return fnmatch.fnmatchcase(value, pattern) if any(c in pattern for c in "*?") else value == pattern


def _prop(item: Item, name: str):
    if name == "NetClass":
        return item.netclass
    if name == "NetName":
        return item.netname
    if name == "Type":
        return item.kind
    if name == "Reference":
        return item.ref
    raise Unsupported(f"property {name}")


def _call(item: Item, name: str, arg: str) -> bool:
    if name == "memberOfFootprint":
        return bool(item.ref) and _wild(item.ref, arg)
    if name == "intersectsCourtyard" or name == "insideCourtyard":
        # where a track lies is not known before it is drawn: only the footprint's own pads count
        return bool(item.ref) and _wild(item.ref, arg)
    if name == "hasNetclass":
        return _wild(item.netclass, arg)
    raise Unsupported(f"function {name}")


def compile_condition(expr: str, uses: Optional[dict] = None):
    """A function (a, b) -> bool for a condition expression. uses, when given,
    collects what the expression looks at: footprint patterns ("refs") and
    whether it asks for an item's type ("kind")."""
    uses = uses if uses is not None else {}
    uses.setdefault("refs", set())
    uses.setdefault("kind", False)
    toks = _tokens(expr)
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take(expected=None):
        t = peek()
        if t is None or (expected and t != expected):
            raise Unsupported(f"expected {expected} in '{expr}'")
        pos[0] += 1
        return t

    def operand():
        t = take()
        if t.startswith("'"):
            s = t[1:-1]
            return lambda a, b: s
        if t[:2] in ("A.", "B.") and len(t) > 2:
            side, name = t[0], t[2:]
            if peek() == "(":
                take("(")
                arg = ""
                if peek() != ")":
                    a_tok = take()
                    if not a_tok.startswith("'"):
                        raise Unsupported(f"argument {a_tok}")
                    arg = a_tok[1:-1]
                take(")")
                _call(Item(), name, arg)            # unknown functions fail now, not per pair
                if name in ("memberOfFootprint", "intersectsCourtyard", "insideCourtyard"):
                    uses["refs"].add(arg)
                return lambda a, b: _call(a if side == "A" else b, name, arg)
            _prop(Item(), name)
            if name == "Type":
                uses["kind"] = True
            elif name == "Reference":
                uses["refs"].add("*")
            return lambda a, b: _prop(a if side == "A" else b, name)
        if t == "(":
            e = disj()
            take(")")
            return e
        if t == "!":
            e = operand()
            return lambda a, b: not e(a, b)
        raise Unsupported(f"token {t}")

    def comparison():
        left = operand()
        if peek() in ("==", "!="):
            op = take()
            right = operand()
            if op == "==":
                return lambda a, b: _eq(left(a, b), right(a, b))
            return lambda a, b: not _eq(left(a, b), right(a, b))
        return left

    def conj():
        e = comparison()
        while peek() == "&&":
            take()
            r = comparison()
            e = (lambda l, r: lambda a, b: l(a, b) and r(a, b))(e, r)
        return e

    def disj():
        e = conj()
        while peek() == "||":
            take()
            r = conj()
            e = (lambda l, r: lambda a, b: l(a, b) or r(a, b))(e, r)
        return e

    fn = disj()
    if peek() is not None:
        raise Unsupported(f"trailing '{peek()}' in '{expr}'")
    return lambda a, b: bool(fn(a, b))


def _eq(x, y) -> bool:
    if isinstance(x, str) and isinstance(y, str):
        return _str_eq(x, y)
    return x == y


@functools.lru_cache(maxsize=1 << 18)
def _str_eq(x: str, y: str) -> bool:
    """== between two strings, either side a wildcard pattern. Cached: a
    board's pair table asks the same (net name, pattern) questions for
    every pair a rule's other side is tried with (44 wildcard rules x 380
    nets spent 16 s in fnmatch at each board read)."""
    return _wild(x, y) or _wild(y, x)


# ---- the rules -----------------------------------------------------------------

OUTER = ("F.Cu", "B.Cu")


def layer_matches(spec: Optional[str], layer: str) -> bool:
    """Does a rule's (layer ...) clause hold on this copper layer (canonical
    name)? None: the rule has no layer clause."""
    if spec is None:
        return True
    s = spec.strip()
    low = s.lower()
    if low == "outer":
        return layer in OUTER
    if low == "inner":
        return layer.endswith(".Cu") and layer not in OUTER
    return _wild(layer, s) or _wild(layer.lower(), low)


@dataclass
class Rule:
    name: str
    test: object             # (a, b) -> bool
    clearance: int           # nm
    layer: Optional[str] = None    # the (layer ...) clause: "outer", "inner", a layer name; None = every layer

    def on(self, layer: Optional[str]) -> bool:
        return layer is None or layer_matches(self.layer, layer)


class ClearanceRules:
    """The clearance rules of a .kicad_dru, with net-class clearances as the fallback."""

    def __init__(self, rules: List[Rule], class_clearance: Dict[str, int], floor: int = 0,
                 skipped: Optional[List[str]] = None, uses: Optional[dict] = None):
        self.rules = rules
        self.uses = uses or {"refs": set(), "kind": False}
        self.class_clearance = class_clearance
        self.floor = floor
        self.skipped = skipped or []
        self._cache: Dict[Tuple[Item, Item], Tuple[int, ...]] = {}
        # Layers the rules tell apart: one "profile" per distinct set of rules
        # that hold on a layer. Rules without a layer clause give one profile.
        self._profiles: List[Tuple[bool, ...]] = []
        self._profile_of: Dict[Optional[str], int] = {}
        self.layered = any(r.layer is not None for r in rules)
        named = sorted({r.layer for r in rules if r.layer is not None and r.layer.lower() not in ("inner", "outer")})
        for name in ["F.Cu", "In1.Cu"] + named:
            self.profile(name)

    @classmethod
    def parse(cls, text: str, class_clearance: Dict[str, int], floor: int = 0) -> "ClearanceRules":
        rules, skipped = [], []
        uses = {"refs": set(), "kind": False}
        for node in _sexpr(text):
            if not isinstance(node, list) or not node or node[0] != "rule":
                continue
            name = node[1] if len(node) > 1 and isinstance(node[1], str) else "?"
            cond, clr, layer = None, None, None
            for part in node[2:]:
                if not isinstance(part, list) or not part:
                    continue
                if part[0] == "condition" and len(part) > 1:
                    cond = part[1]
                elif part[0] == "layer" and len(part) > 1 and isinstance(part[1], str):
                    layer = part[1]
                elif part[0] == "constraint" and len(part) > 1 and part[1] == "clearance":
                    for sub in part[2:]:
                        if isinstance(sub, list) and sub and sub[0] == "min" and len(sub) > 1:
                            clr = sub[1]
            if clr is None:
                continue                      # no clearance in this rule: nothing for the router
            try:
                test = compile_condition(cond, uses) if cond else (lambda a, b: True)
                value = _length(clr)
            except Unsupported as e:
                skipped.append(f"{name}: {e}")
                continue
            rules.append(Rule(name, test, value, layer))
        return cls(rules, class_clearance, floor, skipped, uses)

    def normalise(self, item: Item) -> Item:
        """The item as the rules can tell it apart: the footprint only when a
        rule names it, the type only when a rule asks for it (fewer distinct
        items, fewer pairs to evaluate)."""
        ref = item.ref if item.ref and any(_wild(item.ref, p) for p in self.uses["refs"]) else ""
        kind = item.kind if self.uses["kind"] else "Track"
        return Item(item.netclass, item.netname, ref, kind)

    def _class(self, item: Item) -> int:
        return self.class_clearance.get(item.netclass, self.class_clearance.get("Default", 0))

    def profile(self, layer: Optional[str]) -> int:
        """The index of the layer's rule profile (None: every rule, as a via
        or an item with no layer sees them)."""
        got = self._profile_of.get(layer)
        if got is None:
            prof = tuple(r.on(layer) for r in self.rules) if layer is not None else None
            if prof is None:
                got = -1
            else:
                if prof not in self._profiles:
                    self._profiles.append(prof)
                got = self._profiles.index(prof)
            self._profile_of[layer] = got
        return got

    def rule_between(self, a: Item, b: Item, layer: Optional[str] = None) -> Optional[Rule]:
        """The rule KiCad applies between a and b on this layer (None: the
        last matching rule whatever its layer)."""
        for rule in reversed(self.rules):
            if rule.on(layer) and (rule.test(a, b) or rule.test(b, a)):
                return rule
        return None

    def _per_profile(self, a: Item, b: Item) -> Tuple[int, ...]:
        """The clearance between a and b in every layer profile, each rule's
        condition evaluated at most once."""
        n = len(self._profiles)
        out: List[Optional[int]] = [None] * n
        left = n
        for i in range(len(self.rules) - 1, -1, -1):
            open_ = [k for k in range(n) if out[k] is None and self._profiles[k][i]]
            if not open_:
                continue
            rule = self.rules[i]
            if rule.test(a, b) or rule.test(b, a):
                for k in open_:
                    out[k] = rule.clearance
                left -= len(open_)
                if not left:
                    break
        fallback = max(self._class(a), self._class(b))
        return tuple(max(fallback if v is None else v, self.floor) for v in out)

    def between(self, a: Item, b: Item, layer: Optional[str] = None) -> int:
        """The clearance KiCad's DRC asks between a and b on this copper layer
        (canonical name); with no layer, the strictest over the layers."""
        key = (a, b) if (a.netname, a.ref, a.kind) <= (b.netname, b.ref, b.kind) else (b, a)
        k = self.profile(layer)
        got = self._cache.get(key)
        if got is None or (k >= 0 and k >= len(got)):
            got = self._cache[key] = self._per_profile(a, b)
        return max(got) if k < 0 else got[k]

    @property
    def max_clearance(self) -> int:
        return max([r.clearance for r in self.rules] + list(self.class_clearance.values()) + [self.floor])


# why the last for_board() call found no usable rule in a file that has some
# ("" when it read them, or when there is no file): the board read says so
LAST_PROBLEM = ""


def for_board(board) -> Optional[ClearanceRules]:
    """The board's .kicad_dru next to its file (None when there is none or it
    holds no clearance rule; then LAST_PROBLEM says why, if the file has rules)."""
    global LAST_PROBLEM
    LAST_PROBLEM = ""
    import os
    name = board.GetFileName()
    if not name:
        return None
    path = os.path.splitext(name)[0] + ".kicad_dru"
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    ns = board.GetDesignSettings().m_NetSettings
    classes = {str(n): int(c.GetClearance()) for n, c in ns.GetNetclasses().items()}
    classes["Default"] = int(ns.GetDefaultNetclass().GetClearance())
    rules = ClearanceRules.parse(text, classes, int(board.GetDesignSettings().m_MinClearance))
    if not rules.rules:
        # only rules with a clearance constraint count: a file of fabrication
        # rules (hole sizes, silkscreen, the plugin's own fab presets) has
        # nothing for the router, and losing none of it is no problem
        written = len(re.findall(r"\(\s*constraint\s+clearance\b", _strip_comments(text)))
        if written:
            LAST_PROBLEM = (f"{os.path.basename(path)} has {written} clearance rule(s) but the router could read none "
                            f"({'; '.join(rules.skipped[:3]) or 'unbalanced parentheses?'}): "
                            f"net-class clearances only")
        return None
    return rules
