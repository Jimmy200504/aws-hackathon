from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator


ASCII_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.+#")


class AliasMatch:
    """Small subset of re.Match used by the indexing pipelines."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def group(self, index: int = 0) -> str:
        if index != 0:
            raise IndexError("AliasMatch only contains group 0")
        return self._value


class AliasMatcher:
    """Aho-Corasick alias matcher with regex-compatible boundary semantics.

    A single regex alternation becomes prohibitively slow once Bedrock expands
    the ontology to thousands of aliases. This automaton scans each normalized
    source string once and then emits leftmost-longest, non-overlapping matches,
    matching the behavior of the previous longest-first regex.
    """

    def __init__(self, aliases: Iterable[str]) -> None:
        unique = tuple(dict.fromkeys(alias for alias in aliases if alias))
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._output: list[list[str]] = [[]]
        for alias in unique:
            state = 0
            for character in alias:
                next_state = self._goto[state].get(character)
                if next_state is None:
                    next_state = len(self._goto)
                    self._goto[state][character] = next_state
                    self._goto.append({})
                    self._fail.append(0)
                    self._output.append([])
                state = next_state
            self._output[state].append(alias)

        queue: deque[int] = deque()
        for state in self._goto[0].values():
            queue.append(state)
        while queue:
            state = queue.popleft()
            for character, next_state in self._goto[state].items():
                queue.append(next_state)
                fallback = self._fail[state]
                while fallback and character not in self._goto[fallback]:
                    fallback = self._fail[fallback]
                self._fail[next_state] = self._goto[fallback].get(character, 0)
                inherited = self._output[self._fail[next_state]]
                if inherited:
                    self._output[next_state].extend(inherited)

    @staticmethod
    def _ascii_boundary_ok(text: str, start: int, end: int, alias: str) -> bool:
        if not alias.isascii():
            return True
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        return before not in ASCII_WORD_CHARS and after not in ASCII_WORD_CHARS

    def finditer(self, text: str) -> Iterator[AliasMatch]:
        candidates: list[tuple[int, int, str]] = []
        state = 0
        for end_index, character in enumerate(text):
            while state and character not in self._goto[state]:
                state = self._fail[state]
            state = self._goto[state].get(character, 0)
            for alias in self._output[state]:
                end = end_index + 1
                start = end - len(alias)
                if self._ascii_boundary_ok(text, start, end, alias):
                    candidates.append((start, end, alias))

        # Python regex alternation searches the earliest position first. At the
        # same position our previous alternation was length-descending, and a
        # match consumed overlapping alternatives. Preserve those semantics.
        candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
        cursor = 0
        for start, end, alias in candidates:
            if start < cursor:
                continue
            cursor = end
            yield AliasMatch(alias)
