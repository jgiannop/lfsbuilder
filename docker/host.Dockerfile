# The build host: everything LFS 13.1's chapter 2 asks of the system the
# build starts from, and nothing else. Every build uses this image, so the
# host is the same on every machine.
FROM debian:13

ENV DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash binutils bison coreutils diffutils findutils gawk gcc g++ grep gzip \
        m4 make patch perl python3 sed tar texinfo xz-utils \
        bzip2 ca-certificates docbook-xml file libxml2-utils wget xsltproc \
    && rm -rf /var/lib/apt/lists/* \
    # LFS wants /bin/sh to be bash, and yacc/awk to be bison/gawk.
    && ln -sf bash /bin/sh

# The book asks for a user "lfs" for chapters 5 and 6 (so nothing of the
# host's leaks into the new tools); the runner makes it with the build's own
# user id, so files on the shared folder are the developer's.
WORKDIR /work
