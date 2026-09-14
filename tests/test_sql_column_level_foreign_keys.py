"""列制約で書かれた外部キーの抽出。

SQL は外部キーを2通りで書ける:
  表制約:  FOREIGN KEY (a) REFERENCES parent (id)
  列制約:  a uuid not null references parent (id)   ← FOREIGN KEY 語が無い

列制約は PostgreSQL / MySQL / SQLite いずれでも一般的な書き方であり、
これを取りこぼすと「参照整合性が無い」という誤った所見が出る
（実例: 45本の FK を持つスキーマを「FK数0」と報告した）。
"""

import pytest

from codd.parsing.schemas import _regex_foreign_keys


@pytest.mark.parametrize(
    "label, statement, expected",
    [
        (
            "列制約・参照列あり",
            "create table child (id uuid primary key, parent_id uuid not null references parent (id));",
            1,
        ),
        (
            "列制約・参照列を省略（親の主キーに解決される）",
            "create table child (parent_id uuid references parent);",
            1,
        ),
        (
            "表制約（従来から拾えていた形）",
            "create table child (id uuid, foreign key (parent_id) references parent (id));",
            1,
        ),
        (
            "1行にまとめた定義でも拾う",
            "create table t (id uuid primary key, a_id uuid references a (id), b_id uuid references b (id));",
            2,
        ),
        (
            "引用符つき識別子",
            'create table t ("parent_id" uuid references "parent" ("id"));',
            1,
        ),
        (
            "スキーマ修飾された親テーブル",
            "create table t (tenant_id uuid not null references public.tenant (id));",
            1,
        ),
        (
            "外部キーが無ければ0",
            "create table t (id uuid primary key, name text not null);",
            0,
        ),
    ],
)
def test_foreign_key_count(label: str, statement: str, expected: int) -> None:
    assert len(_regex_foreign_keys(statement, "t")) == expected, label


def test_table_level_and_column_level_are_both_counted() -> None:
    statement = """create table t (
        id uuid primary key,
        a_id uuid not null references a (id),
        b_id uuid references b (id) on delete cascade,
        foreign key (c_id) references c (id)
    );"""
    assert len(_regex_foreign_keys(statement, "t")) == 3


def test_column_level_extraction_records_the_reference() -> None:
    result = _regex_foreign_keys(
        "create table child (parent_id uuid not null references public.parent (id));",
        "child",
    )
    assert result == [
        {
            "name": "",
            "table": "child",
            "columns": ["parent_id"],
            "references_table": "public.parent",
            "references_columns": ["id"],
        }
    ]


def test_comment_before_a_column_does_not_hide_its_foreign_key() -> None:
    """列定義の直前にコメントが挟まっても取りこぼさない。

    "," と列名の間に別行が入ると、コメントを除去しない実装では
    その列の外部キーだけが静かに欠落する。
    """
    statement = """create table facility (
        id uuid primary key default gen_random_uuid(),
        tenant_id uuid not null references tenant (id),
        -- ER図の "||" 側をNOT NULL FKとして表現する
        item_set_id uuid not null references item_set (id),
        /* ブロックコメントでも同じ */
        report_definition_id uuid not null references report_definition (id)
    );"""
    columns = [fk["columns"][0] for fk in _regex_foreign_keys(statement, "facility")]
    assert columns == ["tenant_id", "item_set_id", "report_definition_id"]


def test_references_inside_a_comment_is_not_counted() -> None:
    statement = "create table t (id uuid primary key); -- references nothing"
    assert _regex_foreign_keys(statement, "t") == []


def test_double_dash_inside_a_string_literal_is_not_a_comment() -> None:
    """文字列リテラルの中の "--" を行コメントと誤認すると、後続が丸ごと消える。

    コメント除去を文字列リテラルを跨いで行うと、本PR以前は拾えていた
    表制約の外部キーまで検出できなくなる（回帰）。
    """
    statement = (
        "create table t (sep text default '--', "
        "foreign key (parent_id) references parent (id));"
    )
    assert len(_regex_foreign_keys(statement, "t")) == 1

    column_level = (
        "create table t (sep text default '--', parent_id uuid references parent (id));"
    )
    assert [fk["columns"][0] for fk in _regex_foreign_keys(column_level, "t")] == [
        "parent_id"
    ]


def test_block_comment_opener_inside_a_string_literal_is_not_a_comment() -> None:
    statement = (
        "create table t (glob text default '/*', "
        "foreign key (parent_id) references parent (id));"
    )
    assert len(_regex_foreign_keys(statement, "t")) == 1


def test_doubled_quote_inside_a_string_literal_does_not_end_it() -> None:
    """SQL のエスケープは引用符の二重化。'' を終端と誤ると以降の解釈がずれる。"""
    statement = (
        "create table t (note text default 'it''s -- fine', "
        "parent_id uuid references parent (id));"
    )
    assert [fk["columns"][0] for fk in _regex_foreign_keys(statement, "t")] == [
        "parent_id"
    ]


def test_double_dash_inside_a_quoted_identifier_is_not_a_comment() -> None:
    statement = (
        'create table t ("odd--name" text, '
        "foreign key (parent_id) references parent (id));"
    )
    assert len(_regex_foreign_keys(statement, "t")) == 1


def test_column_name_is_not_taken_from_inside_a_type_parameter_list() -> None:
    """`numeric(10,2)` のカンマを列区切りと誤ると、列名が "2" になる。

    外部キーの本数は合っていても、どの列が親を参照しているかが誤る。
    """
    result = _regex_foreign_keys(
        "create table t (amount numeric(10,2) references currency (code));", "t"
    )
    assert [fk["columns"][0] for fk in result] == ["amount"]


def test_column_name_is_not_taken_from_inside_a_string_default() -> None:
    result = _regex_foreign_keys(
        "create table t (tag text default 'a,b' references taglist (code));", "t"
    )
    assert [fk["columns"][0] for fk in result] == ["tag"]
