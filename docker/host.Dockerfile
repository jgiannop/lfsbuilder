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

# The user "lfs" of chapters 5 and 6 is made by the book's own chapter 4,
# run by lfsbuilder's runner in a container of this image.
WORKDIR /work
