"""
The routing engine, kept free of pcbnew and wx so it can be unit-tested with a
plain Python: where the Freerouting jar and a Java runtime come from, how the
command line is built, how the run is driven, and how its log is turned into
progress.
"""
