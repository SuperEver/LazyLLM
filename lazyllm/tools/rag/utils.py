import os
import shutil
import hashlib
import concurrent
from typing import List, Callable, Generator, Dict, Any, Optional, Union, Tuple, Set
from abc import ABC, abstractmethod
from .index_base import IndexBase
from .doc_node import DocNode
from .global_metadata import RAG_DOC_PATH, RAG_DOC_ID
from lazyllm.common import override
from lazyllm.common.queue import sqlite3_check_threadsafety
import sqlalchemy
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy import Column, insert, update, Row
from sqlalchemy.exc import NoResultFound

import pydantic
import sqlite3
from pydantic import BaseModel
from fastapi import UploadFile
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from filelock import FileLock

import lazyllm
from lazyllm import config

# min(32, (os.cpu_count() or 1) + 4) is the default number of workers for ThreadPoolExecutor
config.add(
    "max_embedding_workers",
    int,
    min(32, (os.cpu_count() or 1) + 4),
    "MAX_EMBEDDING_WORKERS",
)

config.add("default_dlmanager", str, "sqlite", "DEFAULT_DOCLIST_MANAGER")

def gen_docid(file_path: str) -> str:
    return hashlib.sha256(file_path.encode()).hexdigest()


class KBDataBase(DeclarativeBase):
    pass


class KBOperationLogs(KBDataBase):
    __tablename__ = "operation_logs"
    id = Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    log = Column(sqlalchemy.Text, nullable=False)
    created_at = Column(sqlalchemy.DateTime, default=sqlalchemy.func.now(), nullable=False)


DocPartRow = Row
class KBDocument(KBDataBase):
    __tablename__ = "documents"

    doc_id = Column(sqlalchemy.Text, primary_key=True)
    filename = Column(sqlalchemy.Text, nullable=False, index=True)
    path = Column(sqlalchemy.Text, nullable=False)
    created_at = Column(sqlalchemy.DateTime, default=sqlalchemy.func.now(), nullable=False)
    last_updated = Column(sqlalchemy.DateTime, default=sqlalchemy.func.now(), onupdate=sqlalchemy.func.now())
    meta = Column(sqlalchemy.Text, nullable=True)
    status = Column(sqlalchemy.Text, nullable=False, index=True)
    count = Column(sqlalchemy.Integer, default=0)


class KBGroup(KBDataBase):
    __tablename__ = "document_groups"

    group_id = Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    group_name = Column(sqlalchemy.String, nullable=False, unique=True)


class KBGroupDocuments(KBDataBase):
    __tablename__ = "kb_group_documents"

    id = Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    doc_id = Column(sqlalchemy.String, sqlalchemy.ForeignKey("documents.doc_id"), nullable=False)
    group_name = Column(sqlalchemy.String, sqlalchemy.ForeignKey("document_groups.group_name"), nullable=False)
    status = Column(sqlalchemy.Text, nullable=True)
    log = Column(sqlalchemy.Text, nullable=True)
    need_reparse = Column(sqlalchemy.Boolean, default=False, nullable=False)
    # unique constraint
    __table_args__ = (sqlalchemy.UniqueConstraint('doc_id', 'group_name', name='uq_doc_to_group'),)


class DocPathParsingResult(BaseModel):
    doc_id: str
    success: bool
    msg: str
    is_new: bool = False

class DocListManager(ABC):
    DEFAULT_GROUP_NAME = '__default__'
    __pool__ = dict()

    class Status:
        all = 'all'
        waiting = 'waiting'
        working = 'working'
        success = 'success'
        failed = 'failed'
        deleting = 'deleting'
        deleted = 'deleted'

    DELETE_SAFE_STATUS_LIST = [Status.waiting, Status.success, Status.failed]

    def __init__(self, path, name):
        self._path = path
        self._name = name
        self._id = hashlib.sha256(f'{name}@+@{path}'.encode()).hexdigest()
        if not os.path.isabs(path):
            raise ValueError(f"path [{path}] is not an absolute path")

    def __new__(cls, *args, **kw):
        if cls is not DocListManager:
            return super().__new__(cls)
        return super().__new__(__class__.__pool__[config['default_dlmanager']])

    def init_tables(self) -> 'DocListManager':
        if not self.table_inited():
            self._init_tables()
            self.add_kb_group(DocListManager.DEFAULT_GROUP_NAME)

        files_list = []
        for root, _, files in os.walk(self._path):
            files = [os.path.join(root, file_path) for file_path in files]
            files_list.extend(files)
        self.add_files(files_list, status=DocListManager.Status.success)
        return self

    def delete_files(self, file_ids: List[str]) -> List[DocPartRow]:
        document_list = self.update_file_status(
            file_ids, DocListManager.Status.deleting, self.DELETE_SAFE_STATUS_LIST
        )
        safe_delete_ids = [doc.doc_id for doc in document_list]
        self.update_kb_group_file_status(file_ids=safe_delete_ids, status=DocListManager.Status.deleting)
        return document_list

    @abstractmethod
    def table_inited(self): pass

    @abstractmethod
    def _init_tables(self): pass

    @abstractmethod
    def list_files(self, limit: Optional[int] = None, details: bool = False,
                   status: Union[str, List[str]] = Status.all,
                   exclude_status: Optional[Union[str, List[str]]] = None): pass

    @abstractmethod
    def list_all_kb_group(self): pass

    @abstractmethod
    def add_kb_group(self, name): pass

    @abstractmethod
    def list_kb_group_files(self, group: str = None, limit: Optional[int] = None, details: bool = False,
                            status: Union[str, List[str]] = Status.all,
                            exclude_status: Optional[Union[str, List[str]]] = None,
                            upload_status: Union[str, List[str]] = Status.all,
                            exclude_upload_status: Optional[Union[str, List[str]]] = None): pass

    def add_files(
        self,
        files: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        status: Optional[str] = Status.waiting,
        batch_size: int = 64,
    ) -> List[DocPartRow]:
        documents = self._add_files(files, metadatas, status, batch_size)
        if documents:
            self.add_files_to_kb_group([doc.doc_id for doc in documents], group=DocListManager.DEFAULT_GROUP_NAME)
        return documents

    @abstractmethod
    def _add_files(self, files: List[str], metadatas: Optional[List] = None,
                   status: Optional[str] = Status.waiting, batch_size: int = 64) -> List[DocPartRow]: pass

    @abstractmethod
    def delete_obsolete_files(self): pass

    @abstractmethod
    def get_docs_need_reparse(self, group: Optional[str] = None) -> List[KBDocument]: pass

    @abstractmethod
    def get_existing_paths_by_pattern(self, file_path: str) -> List[str]: pass

    @abstractmethod
    def safe_parsing_path(self, file_path: str, content: bytes,
                          metadata: Union[None, Dict[str, Any]] = None) -> DocPathParsingResult: pass

    @abstractmethod
    def update_file_message(self, fileid: str, **kw): pass

    @abstractmethod
    def update_file_status(self, file_ids: List[str], status: str,
                           cond_status_list: Union[None, List[str]] = None) -> List[DocPartRow]: pass

    @abstractmethod
    def add_files_to_kb_group(self, file_ids: List[str], group: str): pass

    @abstractmethod
    def delete_files_from_kb_group(self, file_ids: List[str], group: str): pass

    @abstractmethod
    def get_file_status(self, fileid: str): pass

    @abstractmethod
    def update_kb_group_file_status(self, file_ids: Union[str, List[str]],
                                    status: str, group: Optional[str] = None): pass

    @abstractmethod
    def release(self): pass


class SqliteDocListManager(DocListManager):
    def __init__(self, path, name):
        super().__init__(path, name)
        root_dir = os.path.expanduser(os.path.join(config['home'], '.dbs'))
        os.makedirs(root_dir, exist_ok=True)
        self._db_path = os.path.join(root_dir, f'.lazyllm_dlmanager.{self._id}.db')
        self._db_lock = FileLock(self._db_path + '.lock')
        # ensure that this connection is not used in another thread when sqlite3 is not threadsafe
        self._check_same_thread = not sqlite3_check_threadsafety()
        self._engine = sqlalchemy.create_engine(
            f"sqlite:///{self._db_path}?check_same_thread={self._check_same_thread}"
        )
        self._Session = sessionmaker(bind=self._engine)

    def _init_tables(self):
        KBDataBase.metadata.create_all(bind=self._engine)

    def table_inited(self):
        with self._db_lock, sqlite3.connect(self._db_path, check_same_thread=self._check_same_thread) as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='documents'")
            return cursor.fetchone() is not None

    @staticmethod
    def get_status_cond_and_params(status: Union[str, List[str]],
                                   exclude_status: Optional[Union[str, List[str]]] = None,
                                   prefix: str = None):
        conds, params = [], []
        prefix = f'{prefix}.' if prefix else ''
        if isinstance(status, str):
            if status != DocListManager.Status.all:
                conds.append(f'{prefix}status = ?')
                params.append(status)
        elif isinstance(status, (tuple, list)):
            conds.append(f'{prefix}status IN ({",".join("?" * len(status))})')
            params.extend(status)

        if isinstance(exclude_status, str):
            assert exclude_status != DocListManager.Status.all, 'Invalid status provided'
            conds.append(f'{prefix}status != ?')
            params.append(exclude_status)
        elif isinstance(exclude_status, (tuple, list)):
            conds.append(f'{prefix}status NOT IN ({",".join("?" * len(exclude_status))})')
            params.extend(exclude_status)

        return ' AND '.join(conds), params

    def list_files(self, limit: Optional[int] = None, details: bool = False,
                   status: Union[str, List[str]] = DocListManager.Status.all,
                   exclude_status: Optional[Union[str, List[str]]] = None):
        query = "SELECT * FROM documents"
        params = []
        status_cond, status_params = self.get_status_cond_and_params(status, exclude_status, prefix=None)
        if status_cond:
            query += f' WHERE {status_cond}'
            params.extend(status_params)
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self._db_lock, sqlite3.connect(self._db_path, check_same_thread=self._check_same_thread) as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if details else [row[0] for row in cursor]

    def list_all_kb_group(self):
        with self._db_lock, sqlite3.connect(self._db_path, check_same_thread=self._check_same_thread) as conn:
            cursor = conn.execute("SELECT group_name FROM document_groups")
            return [row[0] for row in cursor]

    def add_kb_group(self, name):
        with self._db_lock, sqlite3.connect(self._db_path, check_same_thread=self._check_same_thread) as conn:
            conn.execute('INSERT OR IGNORE INTO document_groups (group_name) VALUES (?)', (name,))
            conn.commit()

    def list_kb_group_files(self, group: str = None, limit: Optional[int] = None, details: bool = False,
                            status: Union[str, List[str]] = DocListManager.Status.all,
                            exclude_status: Optional[Union[str, List[str]]] = None,
                            upload_status: Union[str, List[str]] = DocListManager.Status.all,
                            exclude_upload_status: Optional[Union[str, List[str]]] = None):
        query = """
            SELECT documents.doc_id, documents.path, documents.status, documents.meta,
                   kb_group_documents.group_name, kb_group_documents.status, kb_group_documents.log
            FROM kb_group_documents
            JOIN documents ON kb_group_documents.doc_id = documents.doc_id
        """
        conds, params = [], []
        if group:
            conds.append('kb_group_documents.group_name = ?')
            params.append(group)

        status_cond, status_params = self.get_status_cond_and_params(status, exclude_status, prefix='kb_group_documents')
        if status_cond:
            conds.append(status_cond)
            params.extend(status_params)

        status_cond, status_params = self.get_status_cond_and_params(
            upload_status, exclude_upload_status, prefix='documents')
        if status_cond:
            conds.append(status_cond)
            params.extend(status_params)

        if conds: query += ' WHERE ' + ' AND '.join(conds)

        if limit:
            query += ' LIMIT ?'
            params.append(limit)

        with self._db_lock, sqlite3.connect(self._db_path, check_same_thread=self._check_same_thread) as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

        if not details: return [row[:2] for row in rows]
        return rows

    def delete_obsolete_files(self):
        with self._db_lock, self._Session() as session:
            docs_to_delete = (
                session.query(KBDocument)
                .filter(KBDocument.status == DocListManager.Status.deleting, KBDocument.count == 0)
                .all()
            )
            for doc in docs_to_delete:
                session.delete(doc)
                log = KBOperationLogs(log=f"Delete obsolete file: {doc.doc_id}")
                session.add(log)
            session.commit()

    def _add_files(self, files: List[str], metadatas: Optional[List[Dict[str, Any]]] = None,
                   status: Optional[str] = DocListManager.Status.waiting, batch_size: int = 64):
        documents = []

        for i in range(0, len(files), batch_size):
            batch_files = files[i:i + batch_size]
            batch_metadatas = metadatas[i:i + batch_size] if metadatas else None
            vals = []

            for i, file_path in enumerate(batch_files):
                doc_id = gen_docid(file_path)

                metadata = batch_metadatas[i].copy() if batch_metadatas else {}
                metadata.setdefault(RAG_DOC_ID, doc_id)
                metadata.setdefault(RAG_DOC_PATH, file_path)

                vals.append(
                    {
                        KBDocument.doc_id.name: doc_id,
                        KBDocument.filename.name: os.path.basename(file_path),
                        KBDocument.path.name: file_path,
                        KBDocument.meta.name: json.dumps(metadata),
                        KBDocument.status.name: status,
                        KBDocument.count.name: 0,
                    }
                )
            with self._db_lock, self._Session() as session:
                rows = session.execute(
                    insert(KBDocument)
                    .values(vals)
                    .prefix_with('OR IGNORE')
                    .returning(KBDocument.doc_id, KBDocument.path)
                ).fetchall()
                session.commit()
                documents.extend(rows)
        return documents

    def get_docs_need_reparse(self, group: str) -> List[KBDocument]:
        with self._db_lock, self._Session() as session:
            filter_status_list = [DocListManager.Status.success, DocListManager.Status.failed]
            documents = (
                session.query(KBDocument).join(KBGroupDocuments, KBDocument.doc_id == KBGroupDocuments.doc_id)
                .filter(KBGroupDocuments.need_reparse.is_(True),
                        KBGroupDocuments.group_name == group,
                        KBGroupDocuments.status.in_(filter_status_list)).all())
            return documents
        return []

    def get_existing_paths_by_pattern(self, pattern: str) -> List[str]:
        exist_paths = []
        with self._db_lock, self._Session() as session:
            docs = session.query(KBDocument).filter(KBDocument.path.like(pattern)).all()
            exist_paths = [doc.path for doc in docs]
        return exist_paths

    def safe_parsing_path(self, file_path: str, content: bytes,
                          metadata: Union[None, Dict[str, Any]] = None) -> DocPathParsingResult:
        doc_id = gen_docid(file_path)

        def gen_result_after_save_file(file_path, content, doc_id, msg, is_new=False):
            with open(file_path, 'wb') as f: f.write(content)
            return DocPathParsingResult(doc_id=doc_id, success=True, msg=msg, is_new=is_new)

        def do_parsing_by_doc_group_records(file_path, content, doc_id, doc_group_records: List[KBGroupDocuments]):
            RowSatus = DocListManager.Status
            if any([doc_group_record.need_reparse for doc_group_record in doc_group_records]):
                return DocPathParsingResult(
                    doc_id=doc_id, success=False, msg="Failed: Document's lasttime reparsing has not been finished")
            elif any([doc_group_record.status == RowSatus.working for doc_group_record in doc_group_records]):
                return DocPathParsingResult(
                    doc_id=doc_id, success=False, msg="Failed: Document is being parsed by kbgroup")
            elif any([doc_group_record.status == RowSatus.deleted for doc_group_record in doc_group_records]):
                return DocPathParsingResult(
                    doc_id=doc_id, success=False, msg="Failed: Unexpected status 'deleted' in KBGroupDocuments")
            else:
                # success, failed, deleting will be handled in worker
                return gen_result_after_save_file(
                    file_path, content, doc_id, "Success: Document will be reparsed in worker thread")

        with self._db_lock, self._Session() as session:
            doc_group_records = session.query(KBGroupDocuments).filter_by(doc_id=doc_id).all()
            docs_existing = session.query(KBDocument).filter_by(doc_id=doc_id).all()
            if len(doc_group_records) == 0:
                # no records in KBGroupDocuments means document has been deleted or never existed
                if len(docs_existing) == 0:
                    return gen_result_after_save_file(file_path, content, doc_id, "Success: Add new doc", True)
                doc_existing = docs_existing[0]
                if doc_existing.status == DocListManager.Status.deleting:
                    session.delete(doc_existing)
                    session.commit()
                    return gen_result_after_save_file(file_path, content, doc_id,
                                                      "Success: Add new doc after deleting", True)
                else:
                    return DocPathParsingResult(
                        doc_id=doc_id, success=False, msg=f"Failed: Document exist with status {doc_existing.status}")
            else:
                # records found in KBGroupDocuments. Check need_reparse flag and status
                doc_path_parsing_result = do_parsing_by_doc_group_records(
                    file_path, content, doc_id, doc_group_records)
                if doc_path_parsing_result.success:
                    for doc_group_record in doc_group_records:
                        doc_group_record.need_reparse = True
                    session.commit()
                return doc_path_parsing_result
        return DocPathParsingResult(doc_id=doc_id, success=False, msg="Failed: Unkknown reason")

    # TODO(wangzhihong): set to metadatas and enable this function
    def update_file_message(self, fileid: str, **kw):
        set_clause = ", ".join([f"{k} = ?" for k in kw.keys()])
        params = list(kw.values()) + [fileid]
        with self._db_lock, sqlite3.connect(self._db_path, check_same_thread=self._check_same_thread) as conn:
            conn.execute(f"UPDATE documents SET {set_clause} WHERE doc_id = ?", params)
            conn.commit()

    def update_file_status(self, file_ids: List[str], status: str,
                           cond_status_list: Union[None, List[str]] = None) -> List[DocPartRow]:
        rows = []
        if cond_status_list is None:
            sql_cond = KBDocument.doc_id.in_(file_ids)
        else:
            sql_cond = sqlalchemy.and_(KBDocument.status.in_(cond_status_list), KBDocument.doc_id.in_(file_ids))
        with self._db_lock, self._Session() as session:
            stmt = (
                update(KBDocument)
                .where(sql_cond)
                .values(status=status)
                .returning(KBDocument.doc_id, KBDocument.path)
            )
            rows = session.execute(stmt).fetchall()
            session.commit()
        return rows

    def add_files_to_kb_group(self, file_ids: List[str], group: str):
        with self._db_lock, self._Session() as session:
            vals = []
            for doc_id in file_ids:
                vals = {
                    KBGroupDocuments.doc_id.name: doc_id,
                    KBGroupDocuments.group_name.name: group,
                    KBGroupDocuments.status.name: DocListManager.Status.waiting,
                }
                rows = session.execute(
                    insert(KBGroupDocuments).values(vals).prefix_with('OR IGNORE').returning(KBGroupDocuments.doc_id)
                ).fetchall()
                session.commit()
                if not rows:
                    continue
                doc = session.query(KBDocument).filter_by(doc_id=rows[0].doc_id).one()
                doc.count += 1
                session.commit()

    def delete_files_from_kb_group(self, file_ids: List[str], group: str):
        with self._db_lock, self._Session() as session:
            for doc_id in file_ids:
                records_to_delete = (
                    session.query(KBGroupDocuments)
                    .filter(KBGroupDocuments.doc_id == doc_id, KBGroupDocuments.group_name == group)
                    .all()
                )
                for record in records_to_delete:
                    session.delete(record)
                session.commit()
                if not records_to_delete:
                    continue
                try:
                    doc = session.query(KBDocument).filter_by(doc_id=records_to_delete[0].doc_id).one()
                    doc.count = max(0, doc.count - 1)
                    session.commit()
                except NoResultFound:
                    lazyllm.LOG.warning(f"No document found for {doc_id}")

    def get_file_status(self, fileid: str):
        with self._db_lock, sqlite3.connect(self._db_path, check_same_thread=self._check_same_thread) as conn:
            cursor = conn.execute("SELECT status FROM documents WHERE doc_id = ?", (fileid,))
        return cursor.fetchone()

    def update_kb_group_file_status(self, file_ids: Union[str, List[str]], status: str, group: Optional[str] = None):
        if isinstance(file_ids, str): file_ids = [file_ids]
        query, params = 'UPDATE kb_group_documents SET status = ? WHERE ', [status]
        if group:
            query += 'group_name = ? AND '
            params.append(group)
        query += f'doc_id IN ({",".join("?" * len(file_ids))})'
        with self._db_lock, sqlite3.connect(self._db_path, check_same_thread=self._check_same_thread) as conn:
            conn.execute(query, (params + file_ids))
            conn.commit()

    def release(self):
        with self._db_lock, sqlite3.connect(self._db_path, check_same_thread=self._check_same_thread) as conn:
            conn.execute('delete from documents')
            conn.execute('delete from document_groups')
            conn.execute('delete from kb_group_documents')
            conn.execute('delete from operation_logs')
            conn.commit()

    def __reduce__(self):
        return (__class__, (self._path, self._name))


DocListManager.__pool__ = dict(sqlite=SqliteDocListManager)


class BaseResponse(BaseModel):
    code: int = pydantic.Field(200, description="API status code")
    msg: str = pydantic.Field("success", description="API status message")
    data: Any = pydantic.Field(None, description="API data")

    class Config:
        json_schema_extra = {
            "example": {
                "code": 200,
                "msg": "success",
            }
        }


def run_in_thread_pool(
    func: Callable,
    params: List[Dict] = [],
) -> Generator:
    tasks = []
    with ThreadPoolExecutor() as pool:
        for kwargs in params:
            thread = pool.submit(func, **kwargs)
            tasks.append(thread)

        for obj in as_completed(tasks):
            yield obj.result()


Default_Suport_File_Types = [".docx", ".pdf", ".txt", ".json"]


def _save_file(_file: UploadFile, _file_path: str):
    file_content = _file.file.read()
    with open(_file_path, "wb") as f:
        f.write(file_content)


def _convert_path_to_underscores(file_path: str) -> str:
    return file_path.replace("/", "_").replace("\\", "_")


def _save_file_to_cache(
    file: UploadFile, cache_dir: str, suport_file_types: List[str]
) -> list:
    to_file_path = os.path.join(cache_dir, file.filename)

    sub_result_list_real_name = []
    if file.filename.endswith(".tar"):

        def unpack_archive(tar_file_path: str, extract_folder_path: str):
            import tarfile

            out_file_names = []
            try:
                with tarfile.open(tar_file_path, "r") as tar:
                    file_info_list = tar.getmembers()
                    for file_info in list(file_info_list):
                        file_extension = os.path.splitext(file_info.name)[-1]
                        if file_extension in suport_file_types:
                            tar.extract(file_info.name, path=extract_folder_path)
                            out_file_names.append(file_info.name)
            except tarfile.TarError as e:
                lazyllm.LOG.error(f"untar error: {e}")
                raise e

            return out_file_names

        _save_file(file, to_file_path)
        out_file_names = unpack_archive(to_file_path, cache_dir)
        sub_result_list_real_name.extend(out_file_names)
        os.remove(to_file_path)
    else:
        file_extension = os.path.splitext(file.filename)[-1]
        if file_extension in suport_file_types:
            if not os.path.exists(to_file_path):
                _save_file(file, to_file_path)
            sub_result_list_real_name.append(file.filename)
    return sub_result_list_real_name


def save_files_in_threads(
    files: List[UploadFile],
    override: bool,
    source_path,
    suport_file_types: List[str] = Default_Suport_File_Types,
):
    real_dir = source_path
    cache_dir = os.path.join(source_path, "cache")

    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)

    for dir in [real_dir, cache_dir]:
        if not os.path.exists(dir):
            os.makedirs(dir)

    param_list = [
        {"file": file, "cache_dir": cache_dir, "suport_file_types": suport_file_types}
        for file in files
    ]

    result_list = []
    for result in run_in_thread_pool(_save_file_to_cache, params=param_list):
        result_list.extend(result)

    already_exist_files = []
    new_add_files = []
    overwritten_files = []

    for file_name in result_list:
        real_file_path = os.path.join(real_dir, _convert_path_to_underscores(file_name))
        cache_file_path = os.path.join(cache_dir, file_name)

        if os.path.exists(real_file_path):
            if not override:
                already_exist_files.append(file_name)
            else:
                os.rename(cache_file_path, real_file_path)
                overwritten_files.append(file_name)
        else:
            os.rename(cache_file_path, real_file_path)
            new_add_files.append(file_name)

    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)
    return (already_exist_files, new_add_files, overwritten_files)

# returns a list of modified nodes
def parallel_do_embedding(embed: Dict[str, Callable], embed_keys: Optional[Union[List[str], Set[str]]],
                          nodes: List[DocNode]) -> List[DocNode]:
    modified_nodes = []
    with ThreadPoolExecutor(config["max_embedding_workers"]) as executor:
        futures = []
        for node in nodes:
            miss_keys = node.has_missing_embedding(embed_keys)
            if not miss_keys:
                continue
            modified_nodes.append(node)
            for k in miss_keys:
                with node._lock:
                    if node.has_missing_embedding(k):
                        future = executor.submit(node.do_embedding, {k: embed[k]}) \
                            if k not in node._embedding_state else executor.submit(node.check_embedding_state, k)
                        node._embedding_state.add(k)
                        futures.append(future)
        if len(futures) > 0:
            for future in concurrent.futures.as_completed(futures):
                future.result()
    return modified_nodes

class _FileNodeIndex(IndexBase):
    def __init__(self):
        self._file_node_map = {}  # Dict[path, Dict[uid, DocNode]]

    @override
    def update(self, nodes: List[DocNode]) -> None:
        for node in nodes:
            path = node.global_metadata.get(RAG_DOC_PATH)
            if path:
                self._file_node_map.setdefault(path, {}).setdefault(node._uid, node)

    @override
    def remove(self, uids: List[str], group_name: Optional[str] = None) -> None:
        for path in list(self._file_node_map.keys()):
            uid2node = self._file_node_map[path]
            for uid in uids:
                uid2node.pop(uid, None)
            if not uid2node:
                del self._file_node_map[path]

    @override
    def query(self, files: List[str]) -> List[DocNode]:
        ret = []
        for file in files:
            nodes = self._file_node_map.get(file)
            if nodes:
                ret.extend(list(nodes.values()))
        return ret

def generic_process_filters(nodes: List[DocNode], filters: Dict[str, Union[str, int, List, Set]]) -> List[DocNode]:
    res = []
    for node in nodes:
        for name, candidates in filters.items():
            value = node.global_metadata.get(name)
            if (not isinstance(candidates, list)) and (not isinstance(candidates, set)):
                if value != candidates:
                    break
            elif (not value) or (value not in candidates):
                break
        else:
            res.append(node)
    return res

def sparse2normal(embedding: Union[Dict[int, float], List[Tuple[int, float]]], dim: int) -> List[float]:
    if not embedding:
        return []

    new_embedding = [0] * dim
    if isinstance(embedding, dict):
        for idx, val in embedding.items():
            new_embedding[int(idx)] = val
    elif isinstance(embedding, list) and isinstance(embedding[0], tuple):
        for pair in embedding:
            new_embedding[int(pair[0])] = pair[1]
    else:
        raise TypeError(f'unsupported embedding datatype `{type(embedding[0])}`')

    return new_embedding

def is_sparse(embedding: Union[Dict[int, float], List[Tuple[int, float]], List[float]]) -> bool:
    if isinstance(embedding, dict):
        return True

    if not isinstance(embedding, list):
        raise TypeError(f'unsupported embedding type `{type(embedding)}`')

    if len(embedding) == 0:
        raise ValueError('empty embedding type is not determined.')

    if isinstance(embedding[0], tuple):
        return True

    if isinstance(embedding[0], float) or isinstance(embedding[0], int):
        return False

    raise TypeError(f'unsupported embedding type `{type(embedding[0])}`')
