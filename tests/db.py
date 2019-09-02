from ephemeral_port_reserve import reserve

import logging
import os
import psycopg2
import random
import shutil
import signal
import sqlite3
import string
import subprocess
import time


class Sqlite3Db(object):
    def __init__(self, path):
        self.path = path

    def get_dsn(self):
        """SQLite3 doesn't provide a DSN, resulting in no CLI-option.
        """
        return None

    def query(self, query):
        orig = os.path.join(self.path)
        copy = self.path + ".copy"
        shutil.copyfile(orig, copy)
        db = sqlite3.connect(copy)

        db.row_factory = sqlite3.Row
        c = db.cursor()
        c.execute(query)
        rows = c.fetchall()

        result = []
        for row in rows:
            result.append(dict(zip(row.keys(), row)))

        db.commit()
        c.close()
        db.close()
        return result

    def execute(self, query):
        db = sqlite3.connect(self.path)
        c = db.cursor()
        c.execute(query)
        db.commit()
        c.close()
        db.close()


class PostgresDb(object):
    def __init__(self, dbname, port):
        self.dbname = dbname
        self.port = port

        self.conn = psycopg2.connect("dbname={dbname} user=postgres host=localhost port={port}".format(
            dbname=dbname, port=port
        ))
        cur = self.conn.cursor()
        cur.execute('SELECT 1')
        cur.close()

    def get_dsn(self):
        return "postgres://postgres:password@localhost:{port}/{dbname}".format(
            port=self.port, dbname=self.dbname
        )

    def query(self, query):
        cur = self.conn.cursor()
        cur.execute(query)

        # Collect the results into a list of dicts.
        res = []
        for r in cur:
            t = {}
            # Zip the column definition with the value to get its name.
            for c, v in zip(cur.description, r):
                t[c.name] = v
            res.append(t)
        cur.close()
        return res

    def execute(self, query):
        with self.conn, self.conn.cursor() as cur:
            cur.execute(query)


class SqliteDbProvider(object):
    def __init__(self, directory):
        self.directory = directory

    def start(self):
        pass

    def get_db(self, node_directory, testname, node_id):
        path = os.path.join(
            node_directory,
            'lightningd.sqlite3'
        )
        return Sqlite3Db(path)

    def stop(self):
        pass


class PostgresDbProvider(object):
    def __init__(self, directory):
        self.directory = directory
        self.port = None
        self.proc = None
        print("Starting PostgresDbProvider")

    def start(self):
        passfile = os.path.join(self.directory, "pgpass.txt")
        self.pgdir = os.path.join(self.directory, 'pgsql')
        # Need to write a tiny file containing the password so `initdb` can pick it up
        with open(passfile, 'w') as f:
            f.write('cltest\n')
        subprocess.check_call([
            '/usr/lib/postgresql/10/bin/initdb',
            '--pwfile={}'.format(passfile),
            '--pgdata={}'.format(self.pgdir),
            '--auth=trust',
            '--username=postgres',
        ])
        self.port = reserve()
        self.proc = subprocess.Popen([
            '/usr/lib/postgresql/10/bin/postgres',
            '-k', '/tmp/',  # So we don't use /var/lib/...
            '-D', self.pgdir,
            '-p', str(self.port),
            '-F',
            '-i',
        ])
        # Hacky but seems to work ok (might want to make the postgres proc a TailableProc as well if too flaky).
        time.sleep(1)
        self.conn = psycopg2.connect("dbname=template1 user=postgres host=localhost port={}".format(self.port))

        # Required for CREATE DATABASE to work
        self.conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)

    def get_db(self, node_directory, testname, node_id):
        # Random suffix to avoid collisions on repeated tests
        nonce = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        dbname = "{}_{}_{}".format(testname, node_id, nonce)

        cur = self.conn.cursor()
        cur.execute("CREATE DATABASE {};".format(dbname))
        cur.close()
        db = PostgresDb(dbname, self.port)
        return db

    def stop(self):
        # Send fast shutdown signal see [1] for details:
        #
        # SIGINT
        #
        # This is the Fast Shutdown mode. The server disallows new connections
        # and sends all existing server processes SIGTERM, which will cause
        # them to abort their current transactions and exit promptly. It then
        # waits for all server processes to exit and finally shuts down. If
        # the server is in online backup mode, backup mode will be terminated,
        # rendering the backup useless.
        #
        # [1] https://www.postgresql.org/docs/9.1/server-shutdown.html
        self.proc.send_signal(signal.SIGINT)
        self.proc.wait()