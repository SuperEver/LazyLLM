import unittest
from lazyllm.tools import SQLiteTool, SqlModule
import lazyllm
import tempfile
from pathlib import Path
import uuid
from lazyllm import LightEngine
import os


class TestSqlTool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        filepath = str(Path(tempfile.gettempdir()) / f"{str(uuid.uuid4().hex)}.db")
        cls.db_filepath = filepath
        with open(filepath, "w") as _:
            pass
        sql_tool = SQLiteTool(filepath)
        tables_info = {
            "employee": {
                "fields": {
                    "employee_id": {
                        "type": "INT",
                        "comment": "工号",
                    },
                    "name": {
                        "type": "TEXT",
                        "comment": "姓名",
                    },
                    "department": {
                        "type": "TEXT",
                        "comment": "部门",
                    },
                }
            },
            "sales": {
                "fields": {
                    "employee_id": {
                        "type": "INT",
                        "comment": "工号",
                    },
                    "q1_2023": {
                        "type": "REAL",
                        "comment": "2023年第1季度销售额",
                    },
                    "q2_2023": {
                        "type": "REAL",
                        "comment": "2023年第2季度销售额",
                    },
                    "q3_2023": {
                        "type": "REAL",
                        "comment": "2023年第3季度销售额",
                    },
                    "q4_2023": {
                        "type": "REAL",
                        "comment": "2023年第4季度销售额",
                    },
                }
            },
        }
        sql_tool.create_tables(tables_info)
        sql_tool.sql_update("INSERT INTO employee VALUES (1, '张三', '销售一部');")
        sql_tool.sql_update("INSERT INTO employee VALUES (2, '李四', '销售二部');")
        sql_tool.sql_update("INSERT INTO sales VALUES (1, 8715.55, 8465.65, 24747.82, 3514.36);")
        sql_tool.sql_update("INSERT INTO sales VALUES (2, 4989.23, 5103.22, 4897.98, 5322.05);")
        cls.sql_tool: SQLiteTool = sql_tool
        # Recommend to use sensenova, gpt-4o, qwen online model
        sql_llm = lazyllm.OnlineChatModule(source="sensenova")
        cls.sql_module: SqlModule = SqlModule(sql_llm, sql_tool, use_llm_for_sql_result=True)

    @classmethod
    def tearDownClass(cls):
        db_path = Path(cls.db_filepath)
        if db_path.is_file():
            db_path.unlink()

    def test_get_talbes(self):
        str_result = self.sql_tool.get_tables_desc()
        self.assertIn("employee", str_result)

    def test_sql_query(self):
        str_results = self.sql_tool.get_query_result_in_json("SELECT department from employee WHERE employee_id=1;")
        self.assertIn("销售一部", str_results)

    def test_llm_query(self):
        # 3. llm chat
        str_results = self.sql_module("去年一整年销售额最多的员工是谁，销售额是多少？")
        print(str_results)
        self.assertIn("张三", str_results)


class TestSqlCallInEngine(unittest.TestCase):
    def test_sql_call(self):
        db_types = ["PostgreSQL"]
        for db_type in db_types:
            env_key = f"LAZYLLM_{db_type.replace(' ', '_')}_URL"
            conn_url = os.environ.get(env_key, None)
            assert conn_url is not None
            resources = [
                dict(id="0", kind="SqlTool", name="sql_tool", args=dict(db_type=db_type, conn_url=conn_url)),
                dict(id="1", kind="OnlineLLM", name="llm", args=dict(source="sensenova")),
            ]
            nodes = [
                dict(
                    id="2",
                    kind="SqlCall",
                    name="sql_call",
                    args=dict(sql_tool="0", llm="1", tables=[], tables_desc="", sql_examples=""),
                )
            ]
            edges = [dict(iid="__start__", oid="2"), dict(iid="2", oid="__end__")]
            engine = LightEngine()
            engine.start(nodes, edges, resources)
            str_answer = engine.run("员工编号是3的人来自哪个部门？")
            print(str_answer)
            assert "销售三部" in str_answer
        pass


if __name__ == "__main__":
    unittest.main()