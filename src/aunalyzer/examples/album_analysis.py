"""
Example of analyzing an album and calculating album gain.
"""

import os
import sys
import math
import statistics
from collections import defaultdict
from aunalyzer import Aunalyzer, AunalyzerException

def calculate_album_gain(track_results):
	"""
	Calculate album gain from individual track results.

	Args:
		track_results: List of AunlzResults objects

	Returns:
		tuple: (album_gain, album_peak)
	"""
	# Extract energy values (linear domain)
	energies = []
	max_peak = 0.0

	for result in track_results:
		# Convert from LUFS to linear energy
		energy = 10 ** (result.ebur128_iloud / 10)
		energies.append(energy)

		# Track maximum peak
		if result.sample_peak > max_peak:
			max_peak = result.sample_peak

	# Calculate mean energy
	if not energies:
		return 0.0, 0.0

	mean_energy = statistics.mean(energies)

	# Convert back to logarithmic domain (LUFS)
	album_loudness = 10 * math.log10(mean_energy)

	# Calculate gain adjustment to reach reference level (-18 LUFS)
	reference_level = -18.0
	album_gain = reference_level - album_loudness

	return album_gain, max_peak

def analyze_album(album_path):
	"""
	Analyze all audio files in a directory as an album.

	Args:
		album_path: Path to directory containing album tracks

	Returns:
		dict: Analysis results including album gain
	"""
	# Find audio files
	audio_extensions = ['.mp3', '.flac', '.ogg', '.wav', '.m4a', 'opus', 'wv']
	audio_files = []

	for file in os.listdir(album_path):
		if any(file.lower().endswith(ext) for ext in audio_extensions):
			audio_files.append(os.path.join(album_path, file))

	if not audio_files:
		print(f"No audio files found in {album_path}")
		return None

	# Sort files by name (usually corresponds to track order)
	audio_files.sort()

	# Analyze each track
	track_results = []
	failed_tracks = []

	for filepath in audio_files:
		try:
			print(f"Analyzing {os.path.basename(filepath)}...")
			result = Aunalyzer.analyze(filepath, do_lra=True)
			track_results.append(result)
		except AunalyzerException as e:
			print(f"Error analyzing {os.path.basename(filepath)}: {e}")
			failed_tracks.append((filepath, str(e)))

	if not track_results:
		print("No tracks were successfully analyzed")
		return None

	# Calculate album gain
	album_gain, album_peak = calculate_album_gain(track_results)

	# Prepare report
	format_counts = defaultdict(int)
	sample_rates = defaultdict(int)
	bit_depths = defaultdict(int)

	for result in track_results:
		format_counts[result.format_name] += 1
		sample_rates[result.sample_rate] += 1
		bit_depths[result.bit_depth] += 1

	# Calculate average loudness and range
	avg_loudness = statistics.mean(result.ebur128_iloud for result in track_results)
	loudness_std = statistics.stdev(result.ebur128_iloud for result in track_results) if len(track_results) > 1 else 0

	# Create report
	album_report = {
		"album_path": album_path,
		"track_count": len(track_results),
		"failed_tracks": len(failed_tracks),
		"formats": dict(format_counts),
		"sample_rates": dict(sample_rates),
		"bit_depths": dict(bit_depths),
		"loudness": {
			"average": avg_loudness,
			"min": min(result.ebur128_iloud for result in track_results),
			"max": max(result.ebur128_iloud for result in track_results),
			"standard_deviation": loudness_std
		},
		"album_gain": album_gain,
		"album_peak": album_peak
	}

	return album_report

if __name__ == "__main__":
	if len(sys.argv) < 2:
		print("Usage: python album_analysis.py <album_directory>")
		sys.exit(1)

	album_path = sys.argv[1]
	if not os.path.isdir(album_path):
		print(f"Error: {album_path} is not a directory")
		sys.exit(1)

	album_report = analyze_album(album_path)

	if album_report:
		# Display album report
		print("\nALBUM ANALYSIS REPORT")
		print("=====================")
		print(f"Album: {os.path.basename(album_path)}")
		print(f"Tracks: {album_report['track_count']} (failed: {album_report['failed_tracks']})")

		print("\nAudio Formats:")
		for fmt, count in album_report['formats'].items():
			print(f"  {fmt}: {count} tracks")

		print("\nTechnical Info:")
		for rate, count in album_report['sample_rates'].items():
			print(f"  {rate} Hz: {count} tracks")

		for depth, count in album_report['bit_depths'].items():
			print(f"  {depth}-bit: {count} tracks")

		print("\nLoudness Information:")
		print(f"  Average: {album_report['loudness']['average']:.2f} LUFS")
		print(f"  Range: {album_report['loudness']['min']:.2f} to {album_report['loudness']['max']:.2f} LUFS")
		print(f"  Standard Deviation: {album_report['loudness']['standard_deviation']:.2f} LU")

		print("\nReplayGain Information:")
		print(f"  Album Gain: {album_report['album_gain']:.2f} dB")
		print(f"  Album Peak: {album_report['album_peak']:.6f}")

		# Recommendation based on loudness
		if album_report['loudness']['average'] < -20:
			print("\nRecommendation: This album is quite quiet compared to modern standards")
		elif album_report['loudness']['average'] > -10:
			print("\nRecommendation: This album is quite loud and may benefit from more dynamic range")

		if album_report['loudness']['standard_deviation'] < 1.0:
			print("The album has very consistent loudness across tracks")
		elif album_report['loudness']['standard_deviation'] > 3.0:
			print("The album has significant loudness variations between tracks")