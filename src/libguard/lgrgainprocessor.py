#
# Copyright (c) 2009-2015 Felix Krull <f_krull@gmx.de>
# Copyright (c) 2019-2020 Christian Haudum <developer@christianhaudum.at>
# Copyright 2020 Nick Kossifidis <mickflemm@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of library guard, a UoC Radio project.
# For more infos visit https://rastapank.radio.uoc.gr
#
# ReplayGain processor, forked from rgain3:
# https://github.com/chaudum/rgain
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

import gi
gi.require_version('Gst', '1.0')
from gi.repository import (
	GLib,
	GObject,
	Gst
)

class LgRgainProcessor(object):

	# Helpers
	def _make_pipeline_element(self, factory_name, name=None):
		element = Gst.ElementFactory.make(factory_name, name)
		if element is None:
			raise LgException(LgErr.ENOGSTPLUGIN, None)
		return element

	def _process_tags(self, msg):
		tags = msg.parse_tag()

		def handle_tag(taglist, tag, userdata):
			if tag == Gst.TAG_TRACK_GAIN:
				_, self.current_track_gain = taglist.get_double(tag)
			elif tag == Gst.TAG_TRACK_PEAK:
				_, self.current_track_peak = taglist.get_double(tag)
			elif tag == Gst.TAG_REFERENCE_LEVEL:
				_, self.current_track_ref_lvl = taglist.get_double(tag)

			elif tag == Gst.TAG_ALBUM_GAIN:
				_, self.album_gain = taglist.get_double(tag)
			elif tag == Gst.TAG_ALBUM_PEAK:
				_, self.album_peak = taglist.get_double(tag)

		tags.foreach(handle_tag, None)
		del tags
        
	def _save_current_track_results(self):
		track_results = LgRgainTrackData(self.current_file, self.current_track_gain,
						 self.current_track_peak, self.current_track_ref_lvl)
		self.current_file = None
		self.current_track_gain = None
		self.current_track_peak = None
		self.current_track_ref_lvl = None
		self.track_results.append(track_results)
		del track_results
		
	def _save_album_results(self):
		album_results = LgRgainAlbumData(self.album_gain, self.album_peak)
		self.album_gain = None
		self.album_peak = None
		self.album_results = album_results
		del album_results

	def _next_file(self):

		if self.files_iter is None:
			raise LgException(LgErr.ERGAIN, None, "No files provided")

		# get the next file
		try:
			fname = next(self.files_iter)
		except StopIteration:
			debug("ReplayGain processing finished")
			self.gloop.quit()
			self._save_album_results()
			return False

		# By default, GLib (and therefore GStreamer) assume any filename to be
		# UTF-8 encoded, regardless of locale settings (though most Unix
		# systems, Linux at least, should be configured for UTF-8 anyways these
		# days). The file name we pass to GStreamer is encoded with the system
		# default encoding here: if that's UTF-8, everyone's happy, if it isn't,
		# GLib's UTF-8 assumption needs to be overridden using the
		# G_FILENAME_ENCODING environment variable (set to locale to tell GLib
		# that all file names passed to it are encoded in the system encoding).
		# That way, people on non-UTF-8 systems or with non-UTF-8 file names can
		# still force all file name processing into a different encoding.

		self.filesrc.set_property("location", fname)
		self.current_file = fname
		debug("ReplayGain processing started for track:\n\t%s", fname)
		del fname
		return True


	# Gstreamer callbacks
	def _on_pad_added(self, decoder, new_pad):
		sinkpad = self.converter.get_compatible_pad(new_pad, None)
		if sinkpad is not None:
			new_pad.link(sinkpad)
			del sinkpad

	def _on_pad_removed(self, decbin, old_pad):
		peer = old_pad.get_peer()
		if peer is not None:
			old_pad.unlink(peer)
			del peer

	def _on_message(self, bus, msg):
		if msg.type == Gst.MessageType.TAG:
			self._process_tags(msg)
		elif msg.type == Gst.MessageType.EOS:
			# Preserve rganalysis state
			self.rgain_analyzer.set_locked_state(True)
			self.pipeline.set_state(Gst.State.NULL)
			# Get results and store them to the list
			self._save_current_track_results()
			ret = self._next_file()
			if ret:
				self.pipeline.set_state(Gst.State.PLAYING)
				# For some reason, GStreamer 1.0's rganalysis element produces
				# an error here unless a flush has been performed.
				pad = self.rgain_analyzer.get_static_pad("src")
				pad.send_event(Gst.Event.new_flush_start())
				pad.send_event(Gst.Event.new_flush_stop(True))
				del pad
			self.rgain_analyzer.set_locked_state(False)
		elif msg.type == Gst.MessageType.ERROR:
			self.pipeline.set_state(Gst.State.NULL)
			err, debug = msg.parse_error()
			msg = err.message
			del err, debug
			self.gloop.quit()
			raise LgException(LgErr.ERGAIN, None, msg)
			

	def _setup_pipeline(self):

		# Initialize pipeline and add/link elements
		pipeline = Gst.Pipeline()
		self.pipeline = pipeline
		
		filesrc = self._make_pipeline_element("filesrc")
		self.filesrc = filesrc
		pipeline.add(filesrc)

		decoder = self._make_pipeline_element("decodebin")
		pipeline.add(decoder)
		filesrc.link(decoder)
		del filesrc

		converter = self._make_pipeline_element("audioconvert")
		pipeline.add(converter)
		decoder.connect("pad-added", self._on_pad_added)
		decoder.connect("pad-removed", self._on_pad_removed)
		del decoder
		
		resampler = self._make_pipeline_element("audioresample")
		pipeline.add(resampler)
		converter.link(resampler)
		self.converter = converter
		del converter

		rgain_analyzer = self._make_pipeline_element("rganalysis")
		rgain_analyzer.set_property("forced", True)
		rgain_analyzer.set_property("reference-level", LgConsts.RGAIN_REF_LVL)
		self.rgain_analyzer = rgain_analyzer
		pipeline.add(rgain_analyzer)
		resampler.link(rgain_analyzer)
		del resampler

		sink = self._make_pipeline_element("fakesink")
		pipeline.add(sink)
		rgain_analyzer.link(sink)
		del rgain_analyzer, sink

		# Listen to the bus for messages
		bus = pipeline.get_bus()
		bus.add_signal_watch()
		bus.connect("message", self._on_message)
		del bus
		del pipeline

	def __init__(self, files = None):
		if files is not None:
			self.files = files
			self.files_iter = iter(self.files)
		else:
			self.files = None
			self.files_iter = None
		self.pipeline = None
		self.filesrc = None
		self.rgain_analyzer = None
		self.converter = None
		self.current_file = None
		self.current_track_gain = None
		self.current_track_peak = None
		self.current_track_ref_lvl = None
		self.album_gain = None
		self.album_peak = None
		self.track_results = list()
		self.album_results = None
		self._setup_pipeline()
		self.gloop = GLib.MainLoop()

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		# This is done on EOS but let's be on the safe side
		self.pipeline.set_state(Gst.State.NULL)
		bus = self.pipeline.get_bus()
		bus.remove_signal_watch()
		self.files.clear()
		del self.files
		del self.files_iter
		del self.pipeline
		del self.filesrc
		del self.rgain_analyzer
		del self.converter
		del self.current_file
		del self.current_track_gain
		del self.current_track_peak
		del self.current_track_ref_lvl
		del self.album_gain
		del self.album_peak
		del self.track_results
		del self.album_results
		del self.gloop
		self.__class__ = _LgDeadRgainProcessor
		return False

	def load_files(self, files):
		if files is not None:
			self.files = files
			self.files_iter = iter(self.files)
		else:
			raise LgException(LgErr.ERGAIN, None, "No files provided")

	def process(self):
		if not self._next_file():
			raise LgException(LgErr.ERGAIN, None, "No files provided")
		self.rgain_analyzer.set_locked_state(False)
		self.rgain_analyzer.set_property("num-tracks", len(self.files))
		self.pipeline.set_state(Gst.State.PLAYING)
		self.gloop.run()
		return self.track_results, self.album_results

class _LgDeadRgainProcessor(LgRgainProcessor):

	def load_files(self):
		raise LgException(LgErr.ERIP, None)

	def process(self):
		raise LgException(LgErr.ERIP, None)
