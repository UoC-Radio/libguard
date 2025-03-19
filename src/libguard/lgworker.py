#
# Copyright 2020 - 2025 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Main entry point / worker
#

import os
from pathlib import Path
from collections import deque
import concurrent.futures
from typing import Deque, Tuple
import itertools

from libguard import LgException, LgErr, LgConsts
from libguard.lgdirectory import LgDirectory, LgAudioDirectory

# Import logging functions directly if they're defined globally
from logging import debug, info, warning, error
import traceback

class LgWorker:

	def __init__(self, options: set, junkyard_path: str, indexer=None,
		     max_dir_workers: int = LgConsts.DEF_DIR_WORKERS, termination_event=None):
		self.options = options
		self.junkyard_path = junkyard_path
		self.junkyard_basename = os.path.basename(self.junkyard_path)
		self.indexer = indexer
		self.max_dir_workers = max_dir_workers
		self.termination_event = termination_event

	def should_terminate(self):
		return self.termination_event is not None and self.termination_event.is_set()

	def process_tree(self, root_entry, recursive=True, pbar=None):

		if not recursive:
			# Just process the root directory directly
			try:
				directory = LgDirectory(root_entry, None, self.options)
				return self._process_directory(directory, pbar=pbar)
			except LgException as err:
				return err.error

		# Set up progress bar if provided - count only level 1 directories
		if pbar is not None:
			try:
				# Count only immediate directories (e.g. artists)
				pbar.total = sum(1 for entry in os.scandir(root_entry) if entry.is_dir())
				pbar.set_description("Overall progress")
			except Exception as err:
				warning("Could not count directories: %s", err)

		# Track the root status
		root_status = LgErr.EOK

		# Current directory queue for a specific parent
		directory_queue: Deque[Tuple[LgDirectory, int]] = deque()  # (directory_object, depth)
		current_parent_path = None
		last_parent_index = 0

		# Check if junkyard is under the root entry
		root_abs_path = os.path.abspath(root_entry)
		junkyard_subpath = None
		if self.junkyard_path.startswith(root_abs_path + os.sep):
			junkyard_subpath = os.path.relpath(self.junkyard_path, root_abs_path)
		del root_abs_path

		# Walk directories bottom-up
		for dirpath, _, _ in os.walk(root_entry, topdown=False):

			# Should we stop ?
			if self.should_terminate():
				debug("Terminating worker loop")
				root_status = LgErr.ETERMINATE
				break

			# Got a new directory, calculate its parent_path and its
			# depth relative to root.
			dir_entry = Path(dirpath)
			debug("Reached %s", dirpath)
			parent_path = os.path.dirname(dirpath)
			rel_path = os.path.relpath(dirpath, root_entry)
			depth = len(rel_path.split(os.sep)) if rel_path != '.' else 0

			# Skip junkyard directory and its subdirs in case it's part of the tree
			if junkyard_subpath is not None:
				if rel_path == junkyard_subpath or rel_path.startswith(junkyard_subpath + os.sep):
					continue
			del rel_path

			# Check if we reached a parent directory (that contains subdirectories
			# already in our queue), or switched from one parent to the other. In
			# the first case we went from e.g. artist1/album1/disc1 to artist1/album1,
			# (so is_parent and parent_changed are both true), in the second case we
			# went from artist1/album1, to artist1/album2 or e.g. from artist1 to
			# artist2/album1/disc1 (so parent_changed is true but is_parent is false).
			# To make this cleaner:
			# vertical move -> is_parent | parent_changed
			# horizontal move -> parent_changed.
			is_parent = directory_queue and dirpath == current_parent_path
			parent_changed = parent_path != current_parent_path

			# Try to create an object for it and see if we get any errors
			try:
				directory = LgDirectory(dir_entry, None, self.options)
			except LgException as err:
				# We should ignore this directory/subtree
				if err.error is LgErr.EIGNORE:
					# If it's a parent directory flush the
					# existing queue before moving on
					if is_parent:
						self._flush_queue(directory_queue)
						info("Ignoring subtree:\n\t%s", dirpath)
					else:
						info("Ignoring subdir:\n\t%s", dirpath)
					continue
				# We can't access this directory, if it has any subdirs, we
				# won't reach them, and they won't be in the queue, so we don't
				# need to do anything extra here, just log a warning and move on.
				elif err.error is LgErr.EACCESS:
					warning("Could not access path:\n\t%s", dirpath)
					continue
				# Something wicked happened, log it as an error for further inspection
				# and move on, we want to process as much of the library as we can.
				else:
					error("Could not create directory object:\n\t%s\n\t%s",
					      dirpath, err)
					continue
			except Exception as err:
				error("Got unhandled exception while creating directory:\n\t%s\n\t%s",
				      dirpath, err)
				traceback.print_exc()
				continue
			del dir_entry

			# We got a directory object without errors (for now), add it to the queue.
			directory_queue.append((directory, depth))
			del depth

			# See if it's a parent directory, and if so update its children already in
			# the queue. If we have another parent dir in there make sure we don't
			# mess with its children or itself, using last_parent_index.
			if is_parent:
				# Make sure we skip the last parent if it exists (we don't want to set
				# its parent too).
				start = last_parent_index + 1 if last_parent_index > 0 else last_parent_index
				end = len(directory_queue) - 1
				for child_directory, _ in itertools.islice(directory_queue, start, end):
					child_directory.set_parent(directory)
				last_parent_index = end

				# We made a vertical move up, if this parent (and its subdirs) are part
				# of a set, do the horizontal move (that comes after continue) and keep
				# adding subdirs/parents until we reach a parent that's not part of a set.
				# To make this cleaner:
				# If we move from artist1/album1/disc1/artwork to artist1/album1/disc1
				# we don't want to process [disc1/artwork,disc1], we want to go to
				# artist1/album1/disc2, disc3 etc (they'll all come before artist1/album1,
				# and they'll al be marked as part of a set), and when we finaly get
				# artist1/album1 (that's not going to be marked as part of a set), we
				# handle the whole set. Note that all non-dirty leaf directories are
				# by default marked as parts of a set (so artwork/info/video), as well
				# as LgDiscDirectory objects (that contain tracks with num_discs and are
				# consistent).
				if directory.is_set_member():
					del is_parent, parent_changed, parent_path, directory
					continue

				if directory_queue:
					debug("Triggering queue processing for:\n\t%s\nQueue: %s", dirpath,
					      [d.get_name() if hasattr(d, 'get_name') else str(d) for d, _ in directory_queue])
					try:
						status = self._process_directory_group(directory_queue, pbar)
						# If this was the root directory, save its status
						if current_parent_path == root_entry:
							root_status = status
						del status
					except Exception as err:
						error("Got unhandled exception while processing directory queue:\n\t%s", err)
						traceback.print_exc()
			del is_parent, directory

			# Update current parent path
			if parent_changed:
				current_parent_path = parent_path
			del parent_path, parent_changed

		# Process any remaining directories in the queue
		if directory_queue:
			debug("Triggering queue processing for remaining dirs")
			status = self._process_directory_group(directory_queue, pbar)
			# Check if this contained the root
			if current_parent_path == root_entry:
				root_status = status
			del status
		del directory_queue, current_parent_path
		return root_status

	def _flush_queue(self, directory_queue: Deque[Tuple[LgDirectory, int]]):
		for directory, _ in directory_queue:
			try:
				directory.__exit__(None, None, None)
			except Exception as err:
				debug("Error cleaning up directory:\n\t%s", err)
		directory_queue.clear()

	def _process_directory_group(self, directory_queue: Deque[Tuple[LgDirectory, int]], pbar=None):

		if not directory_queue:
			return LgErr.EOK

		# Should we stop ?
		if self.should_terminate():
			return LgErr.ETERMINATE

		# Get the parent (or single directory if queue has only one item)
		parent_directory, parent_depth = directory_queue[-1]

		# If the queue contains subdirs and not a single dir,
		# the last one is their parent.
		if len(directory_queue) > 1:
			children = list(directory_queue)[:-1]

			# If we are working on a set with multiple parents and
			# their sub-dirs already in the directory_queue (e.g. album1/disc1/artwork,
			# album1/disc2, album1/disc3/info etc), parent_directory is the top-level parent
			# of the set (album1), and we should update the existing parents (that should
			# not have a parent assigned).
			for child_directory, _ in children:
				if not child_directory.has_parent():
					child_directory.set_parent(parent_directory)

			# Process children in parallel
			if children:
				max_workers = min(self.max_dir_workers, len(children))
				with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
					# Submit all children for processing
					future_to_dir = {
						executor.submit(self._process_directory, directory, pbar): (directory, depth)
						for directory, depth in children
					}

					# Process results as they complete
					for future in concurrent.futures.as_completed(future_to_dir):
						directory, _ = future_to_dir[future]
						try:
							status = future.result()
							# Should we stop ?
							if status == LgErr.ETERMINATE:
								debug("Terminating directory group executor")
								executor.shutdown(cancel_futures=True)
								return LgErr.ETERMINATE
							del status
						except concurrent.futures.CancelledError:
							pass
						except Exception as err:
							error("Got unhandled exception while processing directory queue: %s", err)
							traceback.print_exc()
			del children
		directory_queue.clear()

		# Now process the parent (or the single dir on the list)
		parent_status = self._process_directory(parent_directory, pbar)
		del parent_directory

		# Update progress bar counter only for depth 1 directories and root_entry
		if pbar is not None and (parent_depth == 1 or parent_depth == 0):
			pbar.update(1)
		del parent_depth

		return parent_status

	def _process_directory(self, directory, pbar=None):
		with directory:
			# Should we stop ?
			if self.should_terminate():
				return LgErr.ETERMINATE

			# Check if directory should be withdrawn
			if directory.should_withdraw():
				directory.withdraw(self.junkyard_path)
				return directory.get_withdraw_err()

			# Process the directory
			directory.process()

			# Check again if it should be withdrawn after processing
			if directory.should_withdraw():
				directory.withdraw(self.junkyard_path)
				return directory.get_withdraw_err()
			else:
				# Register with indexer if provided
				if self.indexer is not None:
					directory.register(self.indexer)

			# Update description for all directories to show activity
			if isinstance(directory, LgAudioDirectory):
				if pbar is not None:
					pbar.set_postfix_str(f"Last dir processed: {directory.get_name()}")
					pbar.refresh()

			return LgErr.EOK
