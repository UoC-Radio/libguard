#
# Copyright 2020 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Main entry point / worker
#

import logging
from os import scandir, path
from pathlib import Path
from libguard import LgException, LgErr
from libguard.lgdirectory import LgDirectory

class LgWorker:
	
	@staticmethod
	def run_forest_run(root_dentry, opts, junkyard_path, recursive, pbar = None, indexer = None):

		def forest_step(parent):
			with parent:
				# Process parent (this) directory

				# Got any errors during initialization ?
				# Note that if we have LgErr.EIGNORE or
				# LgErr.EUNKNOWN we 'll get an exception
				# before we end up here.
				if parent.should_withdraw():
					parent.withdraw(junkyard_path)
					return parent.get_withdraw_err()

				# The main thing, errors on arange() and
				# update() are non-fatal, errors on verify()
				# are fatal and lead to withdrawal of this
				# directory from the library.
				parent.arange()
				parent.update()
				parent.verify()

				# Did we get a fatal error above ?
				if parent.should_withdraw():
					parent.withdraw(junkyard_path)
					return parent.get_withdraw_err()

				if not recursive:
					return LgErr.EOK

				# Go through the subdirs recursively
				direntries = scandir(parent.get_path())
				for child_dentry in direntries:
					if child_dentry.is_dir(follow_symlinks = False):
						try:
							child = LgDirectory(child_dentry, parent, opts)
							forest_step(child)
							# Returning from forest_step finalizes
							# dentry, del it from here as well
							del child, child_dentry
						except LgException as err:
							pass
				del direntries

				# Do we need to move this folder to junk due to
				# a failed sub-dir (e.g. this is a multi-disc
				# release and one of the discs failed ) ?
				if parent.should_withdraw():
					parent.withdraw(junkyard_path)
					return parent.get_withdraw_err()

				# Register this directory on the database
				parent.register(indexer)
				return LgErr.EOK

		try:
			root = LgDirectory(root_dentry, None, opts)
		except LgException as err:
			return err.error

		ret = forest_step(root)
		if pbar is not None:
			msg = "Last sub-directory: " + root_dentry.name
			pbar.display(msg, 1)
			del msg
			pbar.update(1)

		del root
		return ret
