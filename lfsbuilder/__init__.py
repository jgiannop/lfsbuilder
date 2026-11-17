"""lfsbuilder: a reproducible, customizable Linux From Scratch build.

A recipe (recipes/*.toml) says what the system is to be; the build reads only
the recipe, the LFS book it names, and the sources the book lists, and runs
every step inside Docker. See README.md.
"""
