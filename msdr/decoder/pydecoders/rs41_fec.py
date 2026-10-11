"""RS41's two interleaved shortened RS(255, 231) codewords.

This is a small, clean-room GF(256) decoder. The field polynomial, roots,
shortening, and interleave follow the public RS41 frame description in
rs1729/RS (see docs/rs41.md). No third-party decoder code is copied here.
"""

from __future__ import annotations

_PRIMITIVE = 0x11D
_NSYM = 24
_N = 255
_K = _N - _NSYM
_MSG_OFFSET = 56


def _tables() -> tuple[tuple[int, ...], tuple[int, ...]]:
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for power in range(255):
        exp[power] = value
        log[value] = power
        value <<= 1
        if value & 0x100:
            value ^= _PRIMITIVE
    for power in range(255, len(exp)):
        exp[power] = exp[power - 255]
    return tuple(exp), tuple(log)


_EXP, _LOG = _tables()


def _mul(a: int, b: int) -> int:
    if not a or not b:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _div(a: int, b: int) -> int:
    if not b:
        raise ZeroDivisionError("division by zero in GF(256)")
    if not a:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


def _eval_ascending(poly: list[int], x: int) -> int:
    value = 0
    for coefficient in reversed(poly):
        value = _mul(value, x) ^ coefficient
    return value


def _syndromes(codeword: list[int]) -> list[int]:
    # The RS41 interleave lays bytes out as ascending polynomial coefficients:
    # parity occupies coefficients 0..23, message follows at coefficient 24.
    # RS41's first root is alpha^0, while the conventional BM recurrence
    # starts at S1. Include S24 so the locator sees the full 24-symbol window.
    return [_eval_ascending(codeword, _EXP[root]) for root in range(_NSYM + 1)]


def _locator(syndromes: list[int]) -> tuple[list[int], int]:
    """Solve the syndrome recurrence for an ascending error locator."""
    # RS41 starts its roots at alpha^0, which makes a conventionally indexed
    # BM implementation easy to shift by one. Solve the at-most-12-term
    # recurrence directly instead: S[n] + sum(C[i] * S[n-i]) == 0.
    values = syndromes[:_NSYM]
    for degree in range(1, _NSYM // 2 + 1):
        matrix = [
            [values[n - i] for i in range(1, degree + 1)] + [values[n]]
            for n in range(degree, 2 * degree)
        ]
        coefficients = _solve_linear(matrix)
        if coefficients is None:
            continue
        if all(
            values[n] ^ _xor_products(coefficients, [values[n - i] for i in range(1, degree + 1)])
            == 0
            for n in range(degree, _NSYM)
        ):
            return [1, *coefficients], degree
    return [1], 0


def _xor_products(left: list[int], right: list[int]) -> int:
    value = 0
    for a, b in zip(left, right, strict=True):
        value ^= _mul(a, b)
    return value


def _solve_linear(matrix: list[list[int]]) -> list[int] | None:
    size = len(matrix)
    for column in range(size):
        pivot = next((row for row in range(column, size) if matrix[row][column]), None)
        if pivot is None:
            return None
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        inverse = _div(1, matrix[column][column])
        matrix[column] = [_mul(value, inverse) for value in matrix[column]]
        for row in range(size):
            if row == column:
                continue
            factor = matrix[row][column]
            if factor:
                matrix[row] = [
                    value ^ _mul(factor, pivot_value)
                    for value, pivot_value in zip(matrix[row], matrix[column], strict=True)
                ]
    return [matrix[row][-1] for row in range(size)]


def _solve_magnitudes(locators: list[int], syndromes: list[int]) -> list[int] | None:
    size = len(locators)
    if not size:
        return []
    matrix = [
        [_EXP[(_LOG[x] * power) % 255] if power else 1 for x in locators] + [syndromes[power]]
        for power in range(size)
    ]
    for column in range(size):
        pivot = next((row for row in range(column, size) if matrix[row][column]), None)
        if pivot is None:
            return None
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        inverse = _div(1, matrix[column][column])
        matrix[column] = [_mul(item, inverse) for item in matrix[column]]
        for row in range(size):
            if row == column:
                continue
            factor = matrix[row][column]
            if factor:
                matrix[row] = [
                    value ^ _mul(factor, pivot_value)
                    for value, pivot_value in zip(matrix[row], matrix[column], strict=True)
                ]
    return [matrix[row][-1] for row in range(size)]


def _correct_codeword(codeword: list[int]) -> tuple[list[int], int] | None:
    syndromes = _syndromes(codeword)
    if not any(syndromes[:_NSYM]):
        return codeword, 0
    locator, degree = _locator(syndromes)
    if not degree or degree > _NSYM // 2:
        return None
    positions: list[int] = []
    locators: list[int] = []
    for position in range(_N):
        x = _EXP[position]
        inverse_x = _EXP[(-_LOG[x]) % 255]
        if _eval_ascending(locator, inverse_x) == 0:
            positions.append(position)
            locators.append(x)
    if len(positions) != degree:
        return None
    magnitudes = _solve_magnitudes(locators, syndromes[:degree])
    if magnitudes is None:
        return None
    corrected = codeword.copy()
    for position, magnitude in zip(positions, magnitudes, strict=True):
        corrected[position] ^= magnitude
    if any(_syndromes(corrected)[:_NSYM]):
        return None
    return corrected, degree


def correct_rs41_frame(frame: bytes) -> tuple[bytes, int] | None:
    """Correct an RS41 standard or extended frame and return bytes/count.

    Invalid lengths, frame markers, or either uncorrectable codeword return
    ``None``. The caller must still require the RS41 sub-block CRCs to pass.
    """
    if len(frame) not in (320, 518):
        return None
    expected_marker = 0x0F if len(frame) == 320 else 0xF0
    padded = bytearray(frame.ljust(518, b"\x00"))
    output = bytearray(frame)
    corrected_count = 0
    for codeword_index in range(2):
        start = 8 + codeword_index * _NSYM
        codeword = list(padded[start : start + _NSYM])
        codeword.extend(padded[_MSG_OFFSET + 2 * i + codeword_index] for i in range(_K))
        result = _correct_codeword(codeword)
        if result is None:
            return None
        corrected, count = result
        corrected_count += count
        transmitted_message_bytes = (len(frame) - _MSG_OFFSET + 1 - codeword_index) // 2
        if any(corrected[_NSYM + transmitted_message_bytes :]):
            # Shortened symbols are fixed zeroes, not received bytes; never
            # allow correction to invent data outside the captured frame.
            return None
        output[start : start + _NSYM] = bytes(corrected[:_NSYM])
        for i, value in enumerate(corrected[_NSYM:]):
            position = _MSG_OFFSET + 2 * i + codeword_index
            if position < len(output):
                output[position] = value
    if output[0x38] != expected_marker:
        return None
    return bytes(output), corrected_count
