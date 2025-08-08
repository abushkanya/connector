import json
import copy
import time
import psycopg2
import uuid
import os
from tabulate import tabulate
import copy

class ConnectorQuery(object):
    """docstring for ConnectorQuery."""

    def _get_all(self, items, colnames = []):
        result = []
        if len(colnames) > 0:
            for data in items:
                _add_data = {}
                i = 0
                for i in range(len(colnames)):
                    _add_data[colnames[i]] = data[i]
                result.append(_add_data)
        else:
            for data in items:
                _add_data = {}
                i = 0
                for i in range(len(self._columns)):
                    _add_data[self._columns[i]] = data[i]
                result.append(_add_data)
        return result
    
    def precheck(self):
        query = "SELECT 1;"
        while True:
            try:
                cursor = self.connection.cursor()
                cursor.execute(query)
                self.connection.commit()
                return True
            except psycopg2.OperationalError:
                time.sleep(0.1)
                self.connector._connect()
                self.connection = self.connector.conn
            except psycopg2.InterfaceError:
                time.sleep(0.1)
                self.connector._connect()
                self.connection = self.connector.conn
    
    def _sql(self, query, arguments, colnames = []):
        self.precheck()
        try:
            cursor = self.connection.cursor()
            cursor.execute(query, arguments)
            self.connection.commit()
            if self._action != "DELETE":
                if colnames:
                    result = self._get_all(cursor.fetchall(), colnames = colnames)
                else:
                    result = self._get_all(cursor.fetchall())
                return result
            return True
        except Exception as e:
            print(e)
            self.connection.rollback()
            return []

    def exec(self):
        _filters = []
        _arguments = ()
        result = []
        for each in self.select_filters:
            _column = each[0]
            _value = each[1]
            _type = each[2]
            if _type == "equal":
                if _value is None:
                    _filters.append(f"{_column} IS NULL")
                else:
                    _filters.append(f"{_column} = %s")
                    _arguments += (_value, )
            if _type == "unequal":
                if _value is None:
                    _filters.append(f"{_column} IS NOT NULL")
                else:
                    _filters.append(f"{_column} <> %s")
                    _arguments += (_value, )
            if _type == "more":
                _filters.append(f"{_column} > %s")
                _arguments += (_value, )
            if _type == "less":
                _filters.append(f"{_column} < %s")
                _arguments += (_value, )
            if _type == "like":
                _filters.append(f"{_column} ILIKE %s")
                _arguments += (f"%{_value}%", )
            if _type == "startswith":
                _filters.append(f"{_column} ILIKE %s")
                _arguments += (f"{_value}%", )
            if _type == "endswith":
                _filters.append(f"{_column} ILIKE %s")
                _arguments += (f"%{_value}", )
            if _type == "contains":
                if isinstance(_value, list):
                    _filters.append(f"%s && {_column}")
                    _arguments += (_value, )
                else:
                    _filters.append(f"%s = ANY({_column})")
                    _arguments += (_value, )
            if _type == "in":
                if isinstance(_value, str):
                    _filters.append(f"'{_column}' = ANY(%s)")
                else:
                    _filters.append(f"{_column} = ANY(%s)")
                # _filters.append(f"{_column} = ANY(%s)")
                _arguments += (_value, )
        # print("filters: ", _filters)
        if self._action == "SELECT":
            _query = f"SELECT {', '.join(self._columns)} FROM {self.tablename}"
            if _filters:
                _query += f" WHERE {' AND '.join(_filters)} "
            if self._order:
                _query += f" ORDER BY {self._order} DESC"
            if self._max_per_page is not None:
                if self._current_page is None:
                    self._current_page = 0
                _query += f" LIMIT {self._max_per_page} OFFSET {self._current_page * self._max_per_page}"
            result = self._sql(_query, _arguments)

        elif self._action == "GROUP_BY":
            if not self._group_bys:
                _query = f"SELECT {self._group_by} "
                colnames = [self._group_by]
            else:
                _query = f"SELECT "
                for each in self._group_bys:
                    _query += f"{each}, "
                _query = _query[:-2]
                colnames = []
                for each in self._group_bys:
                    colnames.append(each)
            for v in self._group_by_args["count"]:
                if v["distinct"]:
                    _query += f", count(distinct {v['name']})"
                _query += f", count({v['name']})"
                colnames.append(f"{v['name']}_count")
            for v in self._group_by_args["summ"]:
                _query += f", sum({v})"
                colnames.append(f"{v}_summ")
            _query += f" FROM {self.tablename}"
            if _filters:
                _query += f" WHERE {' AND '.join(_filters)} "
            if not self._group_bys:
                _query += f" GROUP BY {self._group_by} "
            else:
                _query += f" GROUP BY "
                for each in self._group_bys:
                    _query += f"{each}, "
                _query = _query[:-2]
            if self._order and self._order != "id":
                _query += f" ORDER BY {self._order}"
            if self._max_per_page is not None:
                if self._current_page is None:
                    self._current_page = 0
                _query += f" LIMIT {self._max_per_page} OFFSET {self._current_page * self._max_per_page}"
            result = self._sql(_query, _arguments, colnames = colnames)

        elif self._action == "UPDATE":
            # print("filters: ", _filters)
            _update_columns = []
            _update_arguments = ()
            for each in self.update_filters:
                _column = each[0]
                _value = each[1]
                if _value is None:
                    _update_columns.append(f"{_column} = NULL")
                else:
                    _update_columns.append(f"{_column} = %s")
                    _update_arguments += (_value, )
            if _filters:
                _all_arguments = _update_arguments + _arguments
                _query = f"UPDATE {self.tablename} SET {', '.join(_update_columns)} WHERE {' AND '.join(_filters)} "
                _query = _query + f" RETURNING {', '.join(self._columns)}"
                result = self._sql(_query, _all_arguments)
            else:
                _query = f"UPDATE {self.tablename} SET {', '.join(_update_columns)}"
                _query = _query + f" RETURNING {', '.join(self._columns)}"
                result = self._sql(_query, _update_arguments)
            self.update_filters = []
            self._action = "SELECT"

        elif self._action == "ADD":
            _add_arguments = ()
            columns = []
            for each in self._columns:
                if each != self._primary_column:
                    columns.append(each)
            _query = f"INSERT INTO {self.tablename} ({', '.join(columns)}) VALUES "
            for add in self.adds:
                _col_value = ""
                for each in add:
                    _column = each[0]
                    _add_arguments += (each[1], )
                    if _column not in self._columns:
                        raise ValueError(f"Not found column {_column}")
                    _col_value = _col_value + "%s, "
                _col_value = _col_value[:-2]
                _query += f"({_col_value}), "
            _query = _query[:-2] + f" RETURNING {', '.join(self._columns)}"
            result = self._sql(_query, _add_arguments)
            self.adds = []
            self._action = "SELECT"

        elif self._action == "DELETE":
            _query = f"DELETE FROM {self.tablename} "
            if _filters:
                _query += f" WHERE {' AND '.join(_filters)} "
            result = self._sql(_query, _arguments)
        elif self._action == "COUNT":
            _query = f"SELECT COUNT(*) FROM {self.tablename}"
            if _filters:
                _query += f" WHERE {' AND '.join(_filters)} "
            result = self._sql(_query, _arguments, colnames = ["count"])
            result = result[0]["count"]
        elif self._action == "SUMM":
            _query = f"SELECT SUM({self._col_summname}) FROM {self.tablename}"
            if _filters:
                _query += f" WHERE {' AND '.join(_filters)} "
            result = self._sql(_query, _arguments, colnames = ["summ"])
            result = result[0]["summ"]
            if result == None:
                result = 0
        return result

    def _init_items(self):
        result = []
        for each in self._items:
            result.append(ConnectorQuery(self.connector, self.tablename, is_single = True, unique_identy = each[self._primary_column], single_item = each))
        return result

    @property
    def items(self):
        self._items = self.exec()
        items = self._init_items()
        return items

    @property
    def item(self):
        self._items = self.exec()
        items = self._init_items()
        return items[0]

    def clear_add(self):
        self._action = "SELECT"
        self.adds = []
        return self

    def add_filter(self, filter_type, **kwargs):
        if self._action == "ADD":
            if len(self.adds) > 0:
                raise ValueError("Unadded data. clear them with .clear_add()")
        self._action = "SELECT"
        for k, v in kwargs.items():
            self.select_filters.append((k, v, filter_type))
            if filter_type == "equal":
                if k == self._primary_column:
                    self.is_single = True
                    self.unique_identy = v
        return self

    def get(self, **kwargs):
        return self.add_filter("equal", **kwargs)

    def equal(self, **kwargs):
        return self.add_filter("equal", **kwargs)

    def all(self):
        return self

    def unequal(self, **kwargs):
        return self.add_filter("unequal", **kwargs)

    def more(self, **kwargs):
        return self.add_filter("more", **kwargs)

    def less(self, **kwargs):
        return self.add_filter("less", **kwargs)

    def like(self, **kwargs):
        return self.add_filter("like", **kwargs)

    def startswith(self, **kwargs):
        return self.add_filter("startswith", **kwargs)

    def endswith(self, **kwargs):
        return self.add_filter("endswith", **kwargs)

    def contains(self, **kwargs):
        return self.add_filter("contains", **kwargs)

    def any(self, **kwargs):
        return self.add_filter("in", **kwargs)

    def update(self, **kwargs):
        self._action = "UPDATE"
        for k, v in kwargs.items():
            self.update_filters.append((k, v))
        return self

    def delete(self, **kwargs):
        self.add_filter("equal", **kwargs)
        self._action = "DELETE"
        return self

    def order_by(self, *args):
        self._order = args[0]
        return self

    def group_by(self, *args):
        if args:
            if len(args) > 1:
                self._group_bys = args
            self._group_by = args[0]
            self._group_by_args["count"] = []
        self._group_by_args["summ"] = []
        self._action = "GROUP_BY"
        return self

    def count(self, name = "", distinct = False):
        if not name:
            self._action = "COUNT"
        else:
            self._group_by_args["count"].append({"name": name, "distinct": distinct})
        return self

    def summ(self, name, is_overall = False):
        if is_overall:
            self._action = "SUMM"
            self._col_summname = name
        else:
            self._group_by_args["summ"].append(name)
        return self

    def add(self, **kwargs):
        self._action = "ADD"
        fields = []
        for k in kwargs:
            if k not in self._columns:
                raise ValueError(f"Table has not column {k}")
        for each in self._columns:
            if each in kwargs:
                fields.append((each, kwargs[each]))
            elif each != self._primary_column:
                fields.append((each, None))
        self.adds.append(fields)
        return self

    def page(self, page):
        if self._max_per_page is None:
            raise ValueError("Items per page is not setted")
        self._current_page = page - 1
        return self

    def per_page(self, count):
        self._max_per_page = count
        return self

    def at(self, position):
        return self._items[position]

    def __getitem__(self, key):
        if not self._items:
            self._items = self.exec()
        if type(key) == int:
            return self._items[key]
        elif type(key) == str:
            if self.is_single:
                return self._items[0][key]
            else:
                result = []
                for each in self._items:
                    result.append(each[key])
                return result
        elif type(key) == tuple:
            result = []
            for item in self._items:
                data = {}
                for each in key:
                    data[each] = item[each]
                result.append(data)
            if self.is_single:
                return result[0]
            return result
        elif type(key) == slice:
            return self._items[key.start:key.stop:key.step]

    def copy(self):
        new_copy = ConnectorQuery(self.connector, self.tablename)
        new_copy._columns = self._columns
        new_copy._primary_column = self._primary_column

        new_copy.is_single = copy.deepcopy(self.is_single)
        new_copy.unique_identy = copy.deepcopy(self.unique_identy)
        new_copy._order = copy.deepcopy(self._order)
        new_copy._group_by = copy.deepcopy(self._group_by)
        new_copy._group_by_args = copy.deepcopy(self._group_by_args)
        new_copy._action = copy.deepcopy(self._action)
        new_copy.select_filters = copy.deepcopy(self.select_filters)
        new_copy._max_per_page = copy.deepcopy(self._max_per_page)
        new_copy._current_page = copy.deepcopy(self._current_page)
        new_copy.update_filters = copy.deepcopy(self.update_filters)
        new_copy.adding_fields = copy.deepcopy(self.adding_fields)
        new_copy._items = copy.deepcopy(self._items)
        new_copy.adds = copy.deepcopy(self.adds)
        new_copy.unique_identy = copy.deepcopy(self.unique_identy)
        return new_copy

    def __init__(self, connection, tablename, is_single = False, unique_identy = None, single_item = None):
        super(ConnectorQuery, self).__init__()
        self.connector = connection
        self.connection = connection.conn
        self.tablename = tablename
        self.is_single = is_single
        self.unique_identy = unique_identy

        self._columns = connection.columns[self.tablename]
        self._primary_column = connection.primary_columns[self.tablename]
        self._order = self._primary_column
        self._group_by = None
        self._group_bys = None
        self._group_by_args = {}
        self._action = "SELECT"
        self.select_filters = []
        self._max_per_page = None
        self._current_page = None
        self.update_filters = []
        self.adding_fields = []
        self._items = None
        self._col_summname = self._primary_column
        self.adds = []
        self.unique_identy = unique_identy

        if is_single:
            self.add_filter("equal", **{self._primary_column: unique_identy})
            self._items = [single_item]

    def __repr__(self):

        if self.is_single:
            return f"<Single Item ({self._primary_column}={self.unique_identy})>"
        else:
            if not self.adds:
                return f"<ConnectorQuery({self._action}, SELECT filters: {len(self.select_filters)}, UPDATE filters: {len(self.update_filters)})>"
            else:
                return f"<ConnectorQuery({self._action}, ADD data: {len(self.adds)})>"



    def __str__(self):
        text = f"Action: {self._action}\n"
        if self.is_single:
            text = f"\nSingle Item: {self.is_single} | {self._primary_column}: {self.unique_identy}\n"
        else:
            if self._action in ["SELECT", "UPDATE"]:
                if self._max_per_page:
                    text += f"\nPagination: {self._current_page}-page. Per page: {self._max_per_page}\n"
                if self.select_filters:
                    text += f"\nApplied SELECT filters:\n"
                    text += tabulate(self.select_filters, ["Column", "Filter", "Type"], tablefmt="psql")
                    text += "\n"
                if self.update_filters:
                    text += f"\nApplied UPDATE datas:\n"
                    text += tabulate(self.update_filters, ["Column", "Data"], tablefmt="psql")
                    text += "\n"
            elif self._action == "ADD":
                if self.adds:
                    headers = []
                    table = []
                    for each in self.adds:
                        table_row = []
                        for data in each:
                            if data[0] not in headers:
                                headers.append(data[0])
                            table_row.append(data[1])
                        table.append(table_row)
                    text = f"\nADD datas:\n"
                    text += tabulate(table, headers, tablefmt="psql")
        if self._action == "SELECT":
            if not self._items:
                self._items = self.exec()
        headers = []
        table = []
        if not self._items:
            text += "\nUnexecuted query\n"
            return text
        if self.is_single:
            text += "\nItem (can be unupdated):\n"
            for k, v in self._items[0].items():
                table.append((k, v))
            text += tabulate(table, headers, tablefmt="psql")
            text += "\n"
        else:
            for each in self._items[:1]:
                table_row = []
                for k, v in each.items():
                    if k not in headers:
                        headers.append(k)
                    table_row.append(v)
                table.append(table_row)
            text += "\nItems (can be unupdated):\n"
            text += tabulate(table, headers, tablefmt="psql")
            text += "\n"
        return text


class PostgreSQLConnector(object):
    """docstring for PostgreSQLConnector."""
    # def add_table(self, tablename):
    #     setattr(self, tablename, ConnectorQuery(self.conn, tablename))

    def __getattr__(self, tablename):
        if tablename not in self.tables:
            raise ValueError("No Such table")
        return ConnectorQuery(self, tablename)


    def _load_json(self, json_data):
        f = open(json_data)
        data = json.load(f)
        self.database = data["database"]
        self.user = data["user"]
        self.password = data["password"]
        self.host = data["host"]
        self.port = data["port"]
        self.json_data = data
        return

    def _backup(self):
        try:
            filename = uuid.uuid4().hex
            cur = self.conn.cursor()
            f = open(f'{filename}.sql', 'w')
            _query = "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"
            cursor = self.conn.cursor()
            cursor.execute(_query)
            tables = [i[0] for i in cursor.fetchall()]
            q = ""
            cursor.close()
            for each in tables:
                cur.execute(f'SELECT * FROM {each}')
                for row in cur:
                    if "None" in str(row):
                        row = str(row).replace("None", "Null")
                    if str(row).endswith(",)"):
                        row = str(row).replace(",)", ")")
                    q+=(f"INSERT INTO {each} VALUES {row};\n")
                _query = f"SELECT column_name, column_default FROM information_schema.columns where table_name = '{each}';"
                cursor = self.conn.cursor()
                cursor.execute(_query)
                for i in cursor.fetchall():
                    colname = i[0]
                    default = i[1]
                    break
                cursor.close()
                if default:
                    _query = f"SELECT * FROM {each}_{colname}_seq;"
                    cursor = self.conn.cursor()
                    cursor.execute(_query)
                    last_value = [i[0] for i in cursor.fetchall()]
                    q+=(f"SELECT pg_catalog.setval('public.{each}_id_seq', {last_value[0]}, true);\n")
                    cursor.close()
            f.write(q)
        except psycopg2.DatabaseError as e:
            print(e)
        finally:
            self.backup_file = filename
            return

    def _restore(self, conn, filename):
        try:
            f = open(f'{filename}.sql', 'r')
            _query = f.read()
            conn = conn.conn
            if _query:
                cursor = conn.cursor()
                cursor.execute(_query)
                cursor.close()
                conn.commit()
        except Exception as e:
            print(e)
        return

    def _sync(self, conn):
        try:
            self._backup()
            self._restore(conn, self.backup_file)
            os.remove(f"{self.backup_file}.sql")
        except Exception as e:
            print(e)
        return

    def append_extra_cols(self, cols, colnames):
        result = []
        for lang_cone in self.languages:
            if not colnames:
                cl = copy.deepcopy(cols)
                cl["name"] = cl["name"] + "_" + lang_cone
                cl["langs"] = False
                result.append(cl)
            elif f"{cols['name']}_{lang_cone}" not in colnames:
                cl = copy.deepcopy(cols)
                cl["name"] = cl["name"] + "_" + lang_cone
                cl["langs"] = False
                result.append(cl)
        return result

    def _prepare_with_json(self):
        try:
            if not self.conn:
                raise Exception("Not available connection")
            _query = "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"
            cursor = self.conn.cursor()
            cursor.execute(_query)
            tables = [i[0] for i in cursor.fetchall()]
            _query = ""
            for each in self.json_data["tables"]:
                extra_cols = []
                if each["name"] not in tables:
                    columns = each["columns"]
                    _query += f"CREATE TABLE IF NOT EXISTS {each['name']} ("
                    extra_cols = []
                    for cols in columns:
                        _query += f"{cols['name']} {cols['type']}, "
                        if cols["is_primary"]:
                            _query = _query[:-2] + f" primary key, "
                        if cols["default"]:
                            if isinstance(cols["default"], str):
                                _query = _query[:-2] + f" default '{cols['default']}', "
                            else:
                                _query = _query[:-2] + f" default {cols['default']}, "
                        if cols["not_null"]:
                            _query = _query[:-2] + f" NOT NULL, "
                        if cols["unique"]:
                            _query = _query[:-2] + f" UNIQUE, "
                        if "references" in cols:
                            if cols["references"]["table"] and cols["references"]["column"]:
                                _query = _query[:-2] + f" REFERENCES {cols['references']['table']} ({cols['references']['column']}), "
                        if "langs" in cols:
                            if cols["langs"]:
                                extra_cols += self.append_extra_cols(cols, [])
                    if "langs" in cols:
                        if cols["langs"]:
                            extra_cols += self.append_extra_cols(cols, [])
                    for cols in extra_cols:
                        _query += f"{cols['name']} {cols['type']}, "
                        if cols["is_primary"]:
                            _query = _query[:-2] + f" primary key, "
                        if cols["default"]:
                            if isinstance(cols["default"], str):
                                _query = _query[:-2] + f" default '{cols['default']}', "
                            else:
                                _query = _query[:-2] + f" default {cols['default']}, "
                        if cols["not_null"]:
                            _query = _query[:-2] + f" NOT NULL, "
                        if cols["unique"]:
                            _query = _query[:-2] + f" UNIQUE, "
                        if "references" in cols:
                            if cols["references"]["table"] and cols["references"]["column"]:
                                _query = _query[:-2] + f" REFERENCES {cols['references']['table']} ({cols['references']['column']}), "
                    _query = _query[:-2] + "); \n"
                else:
                    q = f"SELECT * FROM {each['name']} LIMIT 0"
                    cursor.execute(q)
                    colnames = [desc[0] for desc in cursor.description]
                    for cols in each["columns"]:
                        if cols["name"] not in colnames:
                            _query += f"ALTER TABLE {each['name']} ADD COLUMN {cols['name']} {cols['type']}  "
                            if cols["is_primary"]:
                                _query += f" primary key, "
                            if cols["default"]:
                                if isinstance(cols["default"], str):
                                    _query = _query[:-2] + f" default '{cols['default']}', "
                                else:
                                    _query = _query[:-2] + f" default {cols['default']}, "
                            if cols["not_null"]:
                                _query = _query[:-2] + f" NOT NULL, "
                            if cols["unique"]:
                                _query = _query[:-2] + f" UNIQUE, "
                            if "references" in cols:
                                if cols["references"]["table"] and cols["references"]["column"]:
                                    _query = _query[:-2] + f" REFERENCES {cols['references']['table']} ({cols['references']['column']}), "
                            if "langs" in cols:
                                if cols["langs"]:
                                    extra_cols += self.append_extra_cols(cols, colnames)
                        if "langs" in cols:
                            if cols["langs"]:
                                extra_cols += self.append_extra_cols(cols, colnames)
                    for cols in extra_cols:
                        _query += f"ALTER TABLE {each['name']} ADD COLUMN {cols['name']} {cols['type']}; "

                        if cols["is_primary"]:
                            _query = _query[:-2] + f" primary key, "
                        if cols["default"]:
                            if isinstance(cols["default"], str):
                                _query = _query[:-2] + f" default '{cols['default']}', "
                            else:
                                _query = _query[:-2] + f" default {cols['default']}, "
                        if cols["not_null"]:
                            _query = _query[:-2] + f" NOT NULL, "
                        if cols["unique"]:
                            _query = _query[:-2] + f" UNIQUE, "
                        if "references" in cols:
                            if cols["references"]["table"] and cols["references"]["column"]:
                                _query = _query[:-2] + f" REFERENCES {cols['references']['table']} ({cols['references']['column']}), "
                    _query = _query[:-2] + ";\n"
            if _query:
                cursor.execute(_query)
            cursor.close()
            self.conn.commit()
        except Exception as e:
            print(e)
        return

    def _ask_creds(self):
        self.database = input("Database:")
        self.host = input("Host (127.0.0.1):")
        if not self.host:
            self.host = "127.0.0.1"
        self.port = input("Port (25432):")
        if not self.port:
            self.port = "25432"
        self.user = input("User (postgres):")
        if not self.user:
            self.user = "postgres"
        self.password = input("Password (postgres):")
        if not self.password:
            self.password = "postgres"
        return

    def _connect(self):
        self.conn = psycopg2.connect(database = self.database, user = self.user, password = self.password, host = self.host, port = self.port)
        return

    def _init_columns(self):
        cursor = self.conn.cursor()
        q = """SELECT kcu.table_schema, kcu.table_name, string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position) AS pk_columns
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu ON tc.constraint_name   = kcu.constraint_name AND tc.constraint_schema = kcu.constraint_schema AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'PRIMARY KEY' AND kcu.table_schema NOT IN ('information_schema', 'pg_catalog') AND kcu.table_schema NOT LIKE 'pg\_%' ESCAPE '\'
                GROUP BY kcu.table_schema, kcu.table_name ORDER BY kcu.table_schema, kcu.table_name;
                """
        cursor.execute(q)
        for each in cursor.fetchall():
            self.primary_columns[each[1]] = each[2]

        for each in self.tables:
            q = f"SELECT * FROM {each} LIMIT 0"
            cursor.execute(q)
            self.columns[each] = [desc[0] for desc in cursor.description]
        cursor.close()
        return

    def _init_tables(self):
        cursor = self.conn.cursor()
        q = """SELECT table_name
          FROM information_schema.tables
         WHERE table_schema='public' AND table_type='BASE TABLE';"""
        cursor.execute(q)
        for each in cursor.fetchall():
            self.tables.append(each[0])
        cursor.close()
        return

    def __init__(self, config_json = None, database = None, host = "127.0.0.1", port = "25432", user = "postgres", password = "postgres", autoconnect = True, autoreconnect = True):
        super(PostgreSQLConnector, self).__init__()

        self.database = database
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.autoreconnect = autoreconnect
        self.autoconnect = autoconnect

        self._ask = True
        self.is_connected = False
        self.tables = []
        self.columns = {}
        self.primary_columns = {}

        self.json_data = None
        self.conn = None
        self.languages = ['ru', 'en', 'cn']

        if config_json is not None:
            self._load_json(config_json)
            self._connect()
            self._prepare_with_json()
            self._ask = False

        if database is not None:
            self._ask = False

        if self._ask:
            self._ask_creds()

        if self.conn == None or autoconnect:
            self._connect()

        self._init_tables()
        self._init_columns()
        #self._backup()
