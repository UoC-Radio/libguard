"""
Simple usage example for the Aunalyzer module.
"""

import sys
from aunalyzer import Aunalyzer, AunalyzerException

def analyze_and_print(filepath):
	"""
	Analyze an audio file and print the results.
	"""
	try:
		# Analyze the file - returns the C extension's TrackInfo object directly
		result = Aunalyzer.analyze(filepath, do_lra=True)

		# Print analysis results
		print(f"\nAUDIO ANALYSIS RESULTS")
		print(f"=====================")
		print(f"File: {filepath}")
		print(f"Format: {result.format_name}")
		print(f"Sample Rate: {result.sample_rate} Hz")
		print(f"Bit Rate: {result.bit_rate} bps")
		print(f"Bit Depth: {result.bit_depth}-bit")
		print(f"Duration: {result.duration_secs} seconds")
		print(f"Duration difference: {result.duration_diff} seconds")

		# Loudness information
		print(f"\nLoudness Information:")
		print(f"  Integrated Loudness: {result.ebur128_iloud:.2f} LUFS")
		print(f"  Loudness Range: {result.ebur128_lra:.2f} LU")
		print(f"  Sample Peak: {result.sample_peak:.6f}")
		print(f"  ReplayGain 2.0 Adjustment: {result.rg2_gain:.2f} dB")

		return True

	except AunalyzerException as e:
		print(f"\nError analyzing {filepath}")
		print(f"Error: {e}")

		# Check if we have partial information
		if hasattr(e, 'track_info') and e.track_info is not None:
			print("\nPartial information available:")
			print(f"  Format: {e.track_info.format_name}")
			print(f"  Sample Rate: {e.track_info.sample_rate} Hz")
			print(f"  Bit Rate: {e.track_info.bit_rate} bps")
			print(f"  Bit Depth: {e.track_info.bit_depth}-bit")
		print(f"  Duration: {e.track_info.duration_secs} seconds")

		return False

if __name__ == "__main__":
	if len(sys.argv) < 2:
		print("Usage: python simple_usage.py <audio_file>")
		sys.exit(1)

	filepath = sys.argv[1]
	success = analyze_and_print(filepath)

	sys.exit(0 if success else 1)