#
# Copyright 2020-2025 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Library indexer, used for finding duplicate releases/release
# groups on different locations on the library
#

import os
import sqlite3
import contextlib
from logging import debug, info, warning, error
from threading import RLock

from libguard import LgErr, LgException

class LgIndexer:

	def _create_tables(self):
		try:
			with self.db_handle:
				# Table for release groups
				self.db_handle.execute("""
				CREATE TABLE IF NOT EXISTS release_groups (
					id INTEGER PRIMARY KEY,
					releasegroup_id TEXT NOT NULL,
					path TEXT NOT NULL,
					UNIQUE(releasegroup_id, path)
				)
				""")
				
				# Table for albums
				self.db_handle.execute("""
				CREATE TABLE IF NOT EXISTS albums (
					id INTEGER PRIMARY KEY,
					album_id TEXT NOT NULL,
					path TEXT NOT NULL,
					UNIQUE(album_id, path)
				)
				""")
				
				# Create indices for faster lookups
				self.db_handle.execute("CREATE INDEX IF NOT EXISTS idx_releasegroup ON release_groups(releasegroup_id)")
				self.db_handle.execute("CREATE INDEX IF NOT EXISTS idx_album ON albums(album_id)")
				self.db_handle.execute("CREATE INDEX IF NOT EXISTS idx_rg_path ON release_groups(path)")
				self.db_handle.execute("CREATE INDEX IF NOT EXISTS idx_album_path ON albums(path)")
				
		except sqlite3.Error as e:
			error(f"Error creating database tables: {str(e)}")
			raise LgException(LgErr.EDBERR, None, f"Error creating database tables: {str(e)}")

	def __init__(self, db_file):
		self.db_file = db_file
		self.db_handle = None
		self.lock = RLock()  # Using RLock to allow recursive locking from the same thread
		
		try:
			self.db_handle = sqlite3.connect(db_file, check_same_thread=False)
			# Enable foreign keys
			self.db_handle.execute("PRAGMA foreign_keys = ON")
			
			self._create_tables()
		except sqlite3.Error as e:
			error(f"Database initialization error: {str(e)}")
			if self.db_handle:
				self.db_handle.close()
			raise LgException(LgErr.EDBERR, None, f"Database initialization error: {str(e)}")
	
	def __enter__(self):
		return self
	
	def __exit__(self, exc_type, exc_val, exc_tb):
		self.close()
		return False  # Don't suppress exceptions
	
	def close(self):
		if self.db_handle:
			self.db_handle.close()
			self.db_handle = None
	
	def add_album(self, directory_path, releasegroup_id, album_id):
		# Input validation
		if not directory_path:
			error("Cannot add album with empty directory path")
			return
			
		if not releasegroup_id or not album_id:
			error(f"Cannot add album with empty IDs for path: {directory_path}")
			return
			
		# Convert path to string if it's not already
		path = str(directory_path) if not isinstance(directory_path, str) else directory_path
		
		# Use a context manager to ensure lock release
		with self.lock:
			try:
				# First check if exact record already exists to avoid duplicate work
				cursor = self.db_handle.cursor()
				
				# Check for existing exact match in both tables
				cursor.execute(
					"SELECT 1 FROM release_groups WHERE releasegroup_id = ? AND path = ? LIMIT 1",
					(releasegroup_id, path)
				)
				rg_exists = cursor.fetchone() is not None
				
				cursor.execute(
					"SELECT 1 FROM albums WHERE album_id = ? AND path = ? LIMIT 1",
					(album_id, path)
				)
				album_exists = cursor.fetchone() is not None
				
				# If both records exist, nothing to do
				if rg_exists and album_exists:
					debug(f"Album already exists in database: {path}")
					return
				
				# Check for existing release group at different paths
				cursor.execute(
					"SELECT path FROM release_groups WHERE releasegroup_id = ? AND path != ?",
					(releasegroup_id, path)
				)
				existing_rg_paths = cursor.fetchall()
				
				# Check for existing album at different paths
				cursor.execute(
					"SELECT path FROM albums WHERE album_id = ? AND path != ?",
					(album_id, path)
				)
				existing_album_paths = cursor.fetchall()
				
				# Log warnings for duplicate locations
				duplicates_found = False
				
				for row in existing_rg_paths:
					if row and row[0]:  # Ensure the path is not empty
						warning(f"Same release group exists at multiple locations:\n\t{path}\n\t{row[0]}")
						duplicates_found = True
				
				for row in existing_album_paths:
					if row and row[0]:  # Ensure the path is not empty
						warning(f"Same album exists at multiple locations:\n\t{path}\n\t{row[0]}")
						duplicates_found = True
				
				# If duplicates were found, don't add the album again
				if duplicates_found:
					warning(f"Album not added due to existing duplicates: {path}")
					return
				
				# Begin a transaction for inserting/updating records
				with self.db_handle:
					# Add/update release group record (only if needed)
					if not rg_exists:
						cursor.execute(
							"INSERT INTO release_groups (releasegroup_id, path) VALUES (?, ?)",
							(releasegroup_id, path)
						)
					
					# Add/update album record (only if needed)
					if not album_exists:
						cursor.execute(
							"INSERT INTO albums (album_id, path) VALUES (?, ?)",
							(album_id, path)
						)
				
				info(f"Album added to database: {path} (RG: {releasegroup_id}, Album: {album_id})")
				
			except sqlite3.Error as err:
				error(f"Error adding album to database: {str(e)}")
				raise LgException(LgErr.EDBERR, directory_path, f"Error adding album to database: {str(err)}")