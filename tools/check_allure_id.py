#!/usr/bin/env python3
"""
Проверка наличия и корректности декоратора @allure.id для pytest-тестов.

Правила:
- Проверяются только файлы test_*.py.
- Проверяются функции test_* (в том числе методы классов).
- Допустим ровно один декоратор @allure.id над каждой тест-функцией.
- @allure.id должен иметь ровно один позиционный аргумент без kwargs.
- Аргумент — целое положительное число (int) или строка из цифр без ведущих нулей.
- Запрещены: "0", "00123", отрицательные, не-литералы, алиасы и другие варианты (pytest.mark, allure.label и т.п.).
- У каждой тест-функции должен быть декоратор @allure.label("owner", <value>), где <value> — непустая строка.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
import sys

# Коды ошибок для allure.id
AID_MISSING = "AID001"  # отсутствует @allure.id(...)
AID_BAD_ARGC = "AID002"  # не ровно 1 позиционный аргумент
AID_HAS_KW = "AID003"  # есть именованные аргументы
AID_BAD_LITERAL = "AID004"  # аргумент не является допустимым литералом
AID_MULTIPLE = "AID005"  # более одного @allure.id
# Коды ошибок для allure.label("owner", <value>)
AOWN_MISSING = "AOWN001"  # отсутствует @allure.label("owner", ...)
AOWN_EMPTY = "AOWN002"  # пустое/некорректное значение для owner


def is_test_file(path: str) -> bool:
    """Возвращает True, если имя файла соответствует шаблону test_*.py."""
    name = Path(path).name
    return name.startswith("test_") and name.endswith(".py")


def walk_with_parents(tree: ast.AST) -> Iterable[tuple[ast.AST, list[ast.AST]]]:
    """Итератор по AST с сохранением стека родителей каждого узла."""
    stack: list[ast.AST] = []

    def _walk(node: ast.AST) -> Iterable[tuple[ast.AST, list[ast.AST]]]:
        yield node, stack.copy()
        stack.append(node)
        for child in ast.iter_child_nodes(node):
            yield from _walk(child)
        stack.pop()

    yield from _walk(tree)


def is_test_function(node: ast.AST, parents: list[ast.AST]) -> bool:
    """Определяет, является ли узел тест-функцией pytest (имя начинается с test_)."""
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    if not node.name.startswith("test_"):
        return False
    # допускаем верхний уровень и методы классов
    if not parents:
        return True
    parent = parents[-1]
    return isinstance(parent, ast.Module | ast.ClassDef)


def allure_id_calls_from_decorators(func: ast.AST) -> list[ast.Call]:
    """Возвращает список вызовов @allure.id(...) из декораторов функции (строго только allure.id)."""
    calls: list[ast.Call] = []
    for d in func.decorator_list:
        if isinstance(d, ast.Call):
            f = d.func
            # принимаем только @allure.id(...)
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                if f.value.id == "allure" and f.attr == "id":
                    calls.append(d)
    return calls


def allure_owner_label_call(func: ast.AST) -> tuple[ast.Call | None, str | None]:
    """Возвращает (вызов @allure.label/@owner, значение owner) или (None, None)."""
    for dec in getattr(func, "decorator_list", []) or []:
        if not isinstance(dec, ast.Call):
            continue
        f = dec.func
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id == "allure"
            and f.attr == "label"
        ):
            if not (dec.args and isinstance(dec.args[0], ast.Constant) and dec.args[0].value == "owner"):
                continue
            if len(dec.args) >= 2 and isinstance(dec.args[1], ast.Constant) and isinstance(dec.args[1].value, str):
                return dec, dec.args[1].value
            for kw in dec.keywords or []:
                if (
                    kw.arg in ("value", "owner")
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    return dec, kw.value.value
            return dec, ""
        if isinstance(f, ast.Name) and f.id == "owner":
            if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                return dec, dec.args[0].value
            for kw in dec.keywords or []:
                if (
                    kw.arg in ("value", "owner")
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    return dec, kw.value.value
            return dec, ""
    return None, None


def check_allure_id(path: str, node: ast.AST) -> str | None:
    """Проверяет корректность декоратора allure.id у тест-функции."""
    calls = allure_id_calls_from_decorators(node)
    line = node.lineno
    col = node.col_offset

    # Проверка количества декораторов @allure.id
    if len(calls) == 0:
        return err(
            path,
            line,
            col,
            AID_MISSING,
            f"отсутствует @allure.id у теста '{node.name}'",
        )

    if len(calls) > 1:
        c = calls[1]
        return err(
            path,
            getattr(c, "lineno", line),
            getattr(c, "col_offset", col),
            AID_MULTIPLE,
            "над одной тест-функцией должен быть ровно один @allure.id",
        )

    call = calls[0]

    # Ровно один позиционный аргумент, без именованных
    if len(call.args) != 1:
        return err(
            path,
            call.lineno,
            call.col_offset,
            AID_BAD_ARGC,
            "allure.id должен принимать ровно один позиционный аргумент",
        )
    if len(call.keywords) != 0:
        return err(
            path,
            call.lineno,
            call.col_offset,
            AID_HAS_KW,
            "allure.id не должен иметь именованных аргументов",
        )

    arg = call.args[0]
    v = arg.value

    if isinstance(v, str):
        # только цифры, без пробелов/знаков/букв и без ведущих нулей
        if v == "0" or not v.isdigit():
            return err(
                path,
                call.lineno,
                call.col_offset,
                AID_BAD_LITERAL,
                "строка в allure.id должна содержать только цифры и быть больше 0",
            )
        elif len(v) > 1 and v[0] == "0":
            return err(
                path,
                call.lineno,
                call.col_offset,
                AID_BAD_LITERAL,
                "строка в allure.id не должна иметь ведущих нулей",
            )
    else:
        return err(
            path,
            call.lineno,
            call.col_offset,
            AID_BAD_LITERAL,
            "аргумент allure.id должен быть строкой из цифр",
        )


def check_allure_owner(path: str, node: ast.AST) -> str | None:
    """Проверяет наличие и корректность owner-метки у тест-функции."""
    line = node.lineno
    col = node.col_offset
    call, value = allure_owner_label_call(node)
    if call is None and value is None:
        return err(
            path,
            line,
            col,
            AOWN_MISSING,
            f"отсутствует @allure.label(\"owner\", ...) у теста '{node.name}'",
        )

    c_line = getattr(call, "lineno", line)
    c_col = getattr(call, "col_offset", col)
    if value is None or value.strip() == "":
        return err(
            path,
            c_line,
            c_col,
            AOWN_EMPTY,
            (f"пустое или некорректное значение для @allure.label(\"owner\", ...) у теста '{node.name}'"),
        )
    return None


def err(path: str, line: int, col: int, code: str, msg: str) -> str:
    """Форматирует сообщение об ошибке в стиле flake8."""
    return f"{path}:{line}:{col} {code} {msg}"


def check_file(path: str) -> list[str]:
    """Проверяет один файл и возвращает список строк-ошибок."""
    if not is_test_file(path):
        return []

    try:
        src = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return [err(path, 1, 0, "AID000", f"не удалось прочитать файл: {e}")]

    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return [err(path, e.lineno or 1, 0, "AID000", f"синтаксическая ошибка: {e.msg}")]

    errors: list[str] = []

    for node, parents in walk_with_parents(tree):
        if not is_test_function(node, parents):
            continue

        if e := check_allure_id(path, node):
            errors.append(e)

        if e := check_allure_owner(path, node):
            errors.append(e)

    return errors


def main(argv: list[str] | None = None) -> int:
    """CLI-точка входа: принимает список файлов (их даёт pre-commit), печатает ошибки, возвращает код завершения.

    Код 1 — если найдены ошибки, иначе 0.
    """
    argv = sys.argv[1:] if argv is None else argv
    any_err = False
    for p in argv:
        if not p.endswith(".py"):
            continue
        for e in check_file(p):
            any_err = True
            print(e)
    return 1 if any_err else 0


if __name__ == "__main__":
    raise SystemExit(main())
