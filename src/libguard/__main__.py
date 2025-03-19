#!/bin/python3

#
# Copyright 2020-2025 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# Main entry point
#

import sys
import os
import argparse
import threading
import signal
import time
from pathlib import Path
import tempfile
# Import logging functions directly
from logging import basicConfig, info, warning, error, debug
from logging import DEBUG, INFO, WARNING, ERROR

# Import from the libguard package
from libguard.lgworker import LgWorker
from libguard import LgOpts, LgErr
from libguard.lgindexer import LgIndexer
from tqdm import tqdm
import traceback

# Create termination event for graceful shutdown
termination_event = threading.Event()

def signal_handler(sig, frame):
	print('\033[91m'"Shutdown requested. Completing current operations...")
	warning("Shutdown requested. Completing current operations...")
	termination_event.set()

def setup_signal_handlers():
	signal.signal(signal.SIGINT, signal_handler)
	signal.signal(signal.SIGTERM, signal_handler)

def parse_arguments():
	parser = argparse.ArgumentParser(
		description="Library Guardian - Music library organization and verification tool"
	)

	parser.add_argument("library_path", nargs="?", default=".",
			    help="Path to the music library (default: current directory)")

	parser.add_argument("--junkyard", "-j", default="./.junk",
			    help="Path to store invalid/failed files and directories (default: ./.junk)")

	parser.add_argument("--log", "-l", 
			    default=os.path.join(tempfile.gettempdir(), "libguard.log"),
			    help="Path to the log file (default: temporary directory / libguard.log)")

	parser.add_argument("--db", "-d", 
			   default=os.path.join(tempfile.gettempdir(), "libguard_index.db"),
			   help="Path to the database file (default: temporary directory / libguard_index.db)")

	parser.add_argument("--dry-run", "-n", action="store_true",
			    help="Perform a dry run (don't modify files)")

	parser.add_argument("--force-check", "-f", action="store_true",
			    help="Force checking all files (ignore verification timestamps)")

	parser.add_argument("--max-workers", "-w", type=int, default=2,
			    help="Maximum number of worker threads per directory (default: 2)")

	parser.add_argument("--verbose", "-v", action="count", default=0,
			    help="Increase verbosity (can be used multiple times)")

	args = parser.parse_args()

	# Validate paths
	if not os.path.isdir(args.library_path):
		parser.error(f"Library path does not exist or is not a directory: {args.library_path}")

	# Make sure parent directories exist for log and DB
	log_dir = os.path.dirname(args.log)
	if log_dir and not os.path.isdir(log_dir):
		parser.error(f"Log directory does not exist: {log_dir}")

	db_dir = os.path.dirname(args.db)
	if db_dir and not os.path.isdir(db_dir):
		parser.error(f"Database directory does not exist: {db_dir}")

	return args

def main():
	# Setup signal handlers
	setup_signal_handlers()

	# Parse command line arguments
	args = parse_arguments()

	# Set up options based on arguments
	opts = LgOpts(0)  # Start with no options
	if args.dry_run:
		opts |= LgOpts.ODRYRUN
	if args.force_check:
		opts |= LgOpts.OFORCECHECK

	# Set log level based on verbosity
	log_levels = [INFO, DEBUG]  # 0=INFO, 1+=DEBUG
	log_level = log_levels[min(args.verbose, len(log_levels)-1)]

	# Setup logging
	basicConfig(filename=args.log, level=log_level, 
		    format='%(asctime)s - %(levelname)s - %(message)s')


	junkyard_abs_path = os.path.abspath(args.junkyard)


	# Print startup information
	print('\033[96m'"Library Guardian starting...")
	print('\033[95m'"Library path:\t", args.library_path)
	print('\033[95m'"Logfile at:\t", args.log)
	print('\033[95m'"Junkyard:\t", junkyard_abs_path)
	if args.dry_run:
		print('\033[93m'"DRY RUN MODE - No files will be modified")
	start_time = time.monotonic()
	print('\033[92m'"Started on", time.ctime())

	info("Library Guardian starting...")
	info("Library path: %s", args.library_path)
	info("Junkyard: %s", args.junkyard)
	info("Started on %s", time.ctime())
	if args.dry_run:
		info("DRY RUN MODE - No files will be modified")

	# Track success/failure
	exit_code = 0

	try:
		# Make sure junkyard exists
		os.makedirs(args.junkyard, exist_ok=True)

		# Get path entry for the root directory
		abs_path = os.path.realpath(args.library_path)
		root_entry = Path(abs_path)
		# Initialize indexer
		with LgIndexer(args.db) as indexer:
			# Create progress bar
			with tqdm() as pbar:

				# Create worker and pass termination event
				worker = LgWorker(opts, junkyard_abs_path, indexer, max_dir_workers=args.max_workers, termination_event=termination_event)

				# Process the tree
				ret = worker.process_tree(root_entry, recursive=True, pbar=pbar)

				if ret != LgErr.EOK:
					if ret == LgErr.ETERMINATE:
						warning("Processing interrupted by user request")
					else:
						error("Error processing directory: %s", LgErr(ret).name if hasattr(LgErr, 'name') else ret)
				exit_code = ret


	except KeyboardInterrupt:
		# This should be caught by the signal handler, but just in case
		warning("Processing interrupted by keyboard interrupt")
		exit_code = 1

	except Exception as e:
		error("Unexpected error: %s", e)
		print('\033[91m'"Error:", e)
		traceback.print_exc()
		exit_code = 1

	finally:
		# Print completion message
		end_time = time.monotonic()
		elapsed_time = end_time - start_time
		process_time = time.process_time()

		if termination_event.is_set():
			print('\033[93m'"Gracefully shut down after", elapsed_time, "sec, process time:", process_time)
			info("Gracefully shut down after %f sec, process time: %f", elapsed_time, process_time)
		else:
			print('\033[93m'"Finished in", elapsed_time, "sec, process time:", process_time)
			info("Finished in %f sec, process time: %f", elapsed_time, process_time)

	return exit_code

if __name__ == "__main__":
	sys.exit(main())