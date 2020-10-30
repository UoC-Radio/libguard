# Library Guardian

We use this tool to ensure the consistency and integrity of our audio library. It scans the library and looks for corrupted files and incomplete / inconsistent albums, it also ensures that all audio files are tagged with ReplayGain tags and detects duplicate releases / release groups by creating an sqlite database and cross-checking the relevant musicbrainz ids.

The expected directory layout for the library is:

Top level dir -> Artist -> Album (-> disc)

If images or text files are present together with audio files they are being moved to sub-dirs, Artwork (for images) and Info (for text files).

We use musicbrainz picard for tagging with a custom ruleset and a set of custom plugins (we'll upload those at some point on another repo).
