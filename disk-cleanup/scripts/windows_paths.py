"""Lexical checks for ordinary drive paths used in Windows cleanup plans.

Does not resolve 8.3 aliases, links, mount points or filesystem case sensitivity.
"""
import re

DEVICES = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    prefix + number for prefix in ('COM', 'LPT') for number in '123456789¹²³'}


def supported(value):
    if not isinstance(value, str):
        return False
    text = value.replace('/', '\\')
    if not re.match(r'^[A-Za-z]:\\', text):
        return False
    if len(text) == 3:
        return True
    parts = text[3:].split('\\')
    if parts[-1] == '':
        parts.pop()  # One trailing directory separator is unambiguous.
    for part in parts:
        if (not part or part in ('.', '..') or part.endswith((' ', '.'))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or part.split('.', 1)[0].rstrip(' ').upper() in DEVICES):
            return False
    return bool(parts)


def require(value):
    if not supported(value):
        raise ValueError('unsupported Windows path spelling: use an ordinary absolute drive path without device names, streams or ambiguous components')
    return value
