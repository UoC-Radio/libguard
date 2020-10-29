#!/bin/python3

#
# Copyright 2020 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Main entry point
#

from libguard.lgworker import LgWorker
from libguard import LgOpts
import sys
import logging
from gi.repository import Gst
from os import (
	scandir,
	path
)
from pathlib import Path
from libguard import LgErr
from libguard.lgindexer import LgIndexer
import time
import concurrent.futures
from tqdm import tqdm

if __name__ == '__main__':
	#opts = LgOpts.ODRYRUN | LgOpts.OFORCECHECK | LgOpts.OFORCERGAIN
	opts = LgOpts.DEFAULT
	Gst.init()
	log_level = logging.INFO

	if len(sys.argv) > 1 and path.isdir(sys.argv[1]):
		libpath = sys.argv[1]
	else:
		libpath = "."

	if len(sys.argv) > 2 and path.isdir(sys.argv[2]):
		junk_path = sys.argv[2]
	else:
		junk_path = "./.junk"

	if len(sys.argv) > 3 and path.isdir(sys.argv[3]):
		log_path = sys.argv[3]
	else:
		log_path = "/tmp/libguard.log"

	if len(sys.argv) > 4 and path.isdir(sys.argv[4]):
		db_path = sys.argv[4]
	else:
		db_path = "/tmp/libguard_index.db"
		
	print('\033[96m'"Library Guardian starting...")
	print('\033[95m'"Library path:\t", libpath)
	print('\033[95m'"Logfile at:\t", log_path)
	print('\033[95m'"Junkyard:\t", junk_path)
	start_time = time.monotonic()
	print('\033[92m'"Started on", time.ctime())

	logging.basicConfig(filename = log_path, level = log_level)
	logging.info("Library Guardian starting...")
	logging.info("Library path: %s", libpath)
	logging.info("Junkyard: %s", junk_path)
	logging.info("Started on %s", time.ctime())
	# We want to check the given path as well, not only its subdirectories
	# and since scandir() only gives us the contents of the provided dir
	# we need to go one level above and grab the direntry for the provided
	# path.
	abs_path = path.realpath(libpath)
	path_handle = Path(abs_path)
	root_dentry = None
	for entry in scandir(path_handle.parent):
		if entry.path == abs_path:
			root_dentry = entry
			break

	if root_dentry is None:
		logging.error("Couldn't get parent directory !")
		sys.exit(LgErr.EINVPATH)

	# Deal with the provided directory non-recursively first
	ret = LgWorker.run_forest_run(root_dentry, opts, junk_path, False)
	if ret is not LgErr.EOK:
		sys.exit(ret)

	# Deal with its subdirectories, if this is a library directory
	# it should have plenty, so using multiprocessing makes sense.
	with concurrent.futures.ThreadPoolExecutor(max_workers = 4) as executor:
		# For easier debugging sort them alphabeticaly
		direntries = sorted(scandir(root_dentry.path), key=lambda e: e.name)
		with tqdm(total = len(direntries)) as pbar:
			with LgIndexer(db_path) as indexer:
				futures = list()
				for entry in direntries:
					if entry.is_dir():
						futures.append(executor.submit(LgWorker.run_forest_run,
									       entry, opts, junk_path, True, pbar, indexer))
				executor.shutdown(wait=True)
				pbar.display("", 1)

	end_time =  time.monotonic()
	elapsed_time = end_time - start_time
	process_time = time.process_time()
	print('\033[93m'"Finished in", elapsed_time,"sec, process time:", process_time)
	logging.info("Finished in %f sec, process time: %f", elapsed_time, process_time)
	Gst.deinit()
