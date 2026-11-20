"""Bounded literal discovery in a reflection cycle's pinned Git sources."""

import json

from .evolution import source_file


def search_source_page(repo, base, paths, query, *, offset=0, line_offset=0, limit=50, guard=None):
    """Search at most 50 catalog entries; snippets do not count as source inspection."""
    if not isinstance(query, str) or not 1 <= len(query) <= 500:
        raise ValueError("Query must contain 1–500 characters")
    if type(offset) is not int or not 0 <= offset <= len(paths):
        raise ValueError("Offset must be within the matching source paths")
    if type(line_offset) is not int or line_offset < 0:
        raise ValueError("Line offset must be a nonnegative integer")
    if offset == len(paths) and line_offset:
        raise ValueError("No source lines remain at this offset")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Limit must be an integer in 1–100")
    matches, scanned, skipped = [], 0, 0

    def result(index, line=0):
        return {
            "base_commit": base,
            "total_paths": len(paths),
            "matches": matches,
            "scanned_files": scanned,
            "skipped_files": skipped,
            "next_cursor": {"offset": index, "line_offset": line} if index < len(paths) else None,
        }

    end = min(offset + 50, len(paths))
    for index in range(offset, end):
        if guard:
            guard()
        scanned += 1
        first_line = line_offset if index == offset else 0
        try:
            raw = source_file(repo, base, paths[index])
            if raw is None or b"\0" in raw:
                raise ValueError("Not a text source")
            lines = raw.decode("utf-8").splitlines(keepends=True)
        except ValueError:
            if first_line:
                raise ValueError("Cannot resume lines in a skipped source") from None
            skipped += 1
            continue
        if first_line > len(lines):
            raise ValueError("Line offset exceeds source length")
        char_offset = sum(map(len, lines[:first_line]))
        for number in range(first_line, len(lines)):
            line = lines[number].splitlines()[0]
            position = line.find(query)
            if position >= 0:
                before = min(100, 500 - len(query))
                start = min(max(0, position - before), max(0, len(line) - 500))
                match = {
                    "path": paths[index],
                    "line": number + 1,
                    "column": position + 1,
                    "offset": char_offset + start,
                    "text": line[start : start + 500],
                    "text_truncated": len(line) > 500,
                }
                matches.append(match)
                # Reserve space for final scan counters and cursor digit changes.
                if len(json.dumps(result(index, number), ensure_ascii=False)) > 17000:
                    matches.pop()
                    if not matches:
                        raise ValueError("Source match metadata exceeds output budget")
                    return result(index, number)
                if len(matches) == limit:
                    if number + 1 < len(lines):
                        return result(index, number + 1)
                    return result(index + 1)
            char_offset += len(lines[number])
    return result(end)
