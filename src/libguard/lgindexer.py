#
# Copyright 2020 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Library indexer, used for finding duplicate releases/release
# groups on different locations on the library
#

from logging import (
	debug,
	info,
	warning,
	error
)
from libguard import (
	LgFormats,
	LgConsts,
	LgOpts,
	LgErr,
	LgException,
	LgRgainTrackData,
	LgRgainAlbumData
)
import sqlite3
from sqlite3 import Error
from threading import Lock

class LgIndexer(object):

	def __init__(self, dbfile):
		self.db_handle = None
		self.lock = Lock()

		try:
			self.db_handle = sqlite3.connect(dbfile, check_same_thread=False)
		except Error as e:
			raise LgException(LgErr.EDBERR, None, str(e))

		try:
			cur = self.db_handle.cursor()
			cur.execute("CREATE TABLE IF NOT EXISTS albums (id INTEGER PRIMARY KEY, path TEXT, name TEXT, releasegroup_id TEXT, album_id TEXT)")
		except Error as e:
			raise LgException(LgErr.EDBERR, None, str(e))

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		if self.db_handle is not None:
			self.db_handle.close()
		del self.db_handle
		return True

	def add_album(self, dentry, releasegroup_id, album_id):
		# Check if album exists
		exists = False
		try:
			cur = self.db_handle.cursor()
			query = "SELECT path, album_id FROM albums WHERE releasegroup_id = ?"
			args = (releasegroup_id,)
			cur.execute(query, args)
			results = cur.fetchall()
			for result in results:
				if result[0] == dentry.path:
					debug("Album already exists on database (%s):\n\t%s",
					      releasegroup_id, dentry.path)
					exists = True
				elif len(result) == 2 and result[1] == album_id:
					error("Same album exists on multiple locations:\n\t%s\n\t%s",
					      dentry.path, result[0])
				else:
					warning("Multiple releases of the same group:\n\t%s\n\t%s",
						dentry.path, result[0])
			results.clear()
			del cur, query, args, results

		except Error as err:
			debug("Got database error: %s", str(err))
			raise LgException(LgErr.EDBERR, dentry, str(err))

		if exists is True:
			return

		# We need to serialize access to the db to avoid corruption
		self.lock.acquire()
		try:
			cur = self.db_handle.cursor()
			query = "INSERT INTO albums(path, name, releasegroup_id) VALUES(?, ?, ?)"
			args = (dentry.path, dentry.name, releasegroup_id)
			cur.execute(query, args)
			self.db_handle.commit()
			del cur, query, args
		except Error as err:
			debug("Got database error: %s", str(err))
			self.lock.release()
			raise LgException(LgErr.EDBERR, dentry, str(err))

		self.lock.release()
		debug("Album added to database (%s):\n\t%s", releasegroup_id, dentry.path)
