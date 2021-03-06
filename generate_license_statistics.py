#!/usr/bin/env python2

"""
Generate statistics on package licenses in Debian.
Released under AGPLv3.

Author: Federico Ceratto <federico@firelet.net>

Usage: run the script, look at the charts.
 Careful with not hammering the http://snapshot.debian.org/ and
 http://metadata.ftp-master.debian.org by querying many thousands
 of packages!

"""

from argparse import ArgumentParser
from beaker.cache import CacheManager
from collections import Counter
from datetime import date
import debian.copyright
import json
import logging
import os.path
import pandas as pd
import random
import re
import requests
import time

CACHE_DIR = '.cache'
MAX_QPS = 20  # queries per second against metadata.ftp-master

log = logging.getLogger(__name__)


# Ugly license guesswork ahead

guessers = (
    ('Creative Commons Attribution-ShareAlike', 'CC'),
    ('/usr/share/common-licenses/BSD', 'BSD'),
    ('LaTeX Project Public License', 'LPPL'),
    ('Permission is hereby granted, free of charge, \
to any person obtaining a copy', 'MIT'),
    ('under the "Artistic" license', 'Artistic'),
    ('/usr/share/common-licenses/Apache-2.0', 'Apache-2.0'),
    ('/usr/share/common-licenses/BSD', 'BSD'),
    ("""GNU General Public License as published by
the Free Software Foundation; either version 2""", 'GPL-2'),
    ('may be used to endorse or promote products', 'BSD'),
    ('/usr/share/common-licenses/GPL-3', 'GPL-3'),
    ('/usr/share/common-licenses/GPL-[^3]', 'GPL-2'),
    ('/usr/share/common-licenses/GPL[^-]', 'GPL-2'),
    ('/usr/share/common-licenses/LGPL-3', 'LGPL-3'),
    ('/usr/share/common-licenses/LGPL-[^3]', 'LGPL-2'),
    ('/usr/share/common-licenses/LGPL[^-]', 'LGPL-2'),
    ('/usr/share/common-licenses/Artistic', 'Artistic'),
    ('from the Public Domain or from', 'Artistic'),
    ('modifications in the Public Domain or otherwise', 'Artistic'),
    ('GNU Free Documentation License', 'GFDL'),
    ('under the terms of the GPL', 'GPL-2'),

    # less common licenses, grouped together as "other"
    ('9menu is free software', 'other'),
    ('Allegro is gift-ware', 'other'),
    ("Ruby's License", 'other'),
    ('QoSient Public License', 'other'),
)

known_licenses = {
    'Allegro-gift-ware': 'Allegro',
    'Apache-2.0': 'Apache-2.0',
    'Artistic or GPL-1': 'Artistic',
    'Artistic-2.0': 'Artistic-2',
    'BSD': 'BSD',
    'BSD-3-clause': 'BSD',
    'BSD-like': 'BSD',
    'CC-BY-SA-3.0': 'CC-BY-SA-3',
    'CeCILL-C': 'CeCILL-C',
    'EPL': 'EPL',
    'Expat': 'Expat',
    'GNU Lesser GPL v3': 'LGPL-3',
    'GPL-2': 'GPL-2',
    'GPL-2 and LGPL-2.1': 'LGPL-2',
    'GPL-2 and Other': 'GPL-2',
    'GPL-2.0': 'GPL-2',
    'GPL-2+ with OpenSSL exemption': 'GPL-2',
    'GPL-2+ with Autoconf exception': 'GPL-2',
    'GPL-3': 'GPL-3',
    'GPL-3.0': 'GPL-3',
    'GPL2': 'GPL-2',
    'GPL3': 'GPL-3',
    'ISC': 'ISC',
    'LGPL': 'LGPL',
    'LGPL-2': 'LGPL-2',
    'LGPL-2.1': 'LGPL-2',
    'LGPLv2.1': 'LGPL-2',
    'LGPL-3': 'LGPL-3',
    'MIT': 'MIT',
    'MPL-1.1': 'MPL-1.1',
    'PD': 'PD',
    'Public_Domain_1': 'PD',
    'Zlib': 'Zlib',
    'other-BSD': 'BSD',
    'public-domain': 'PD',
}

# Enable on-disk caching

cache = CacheManager(
    data_dir=os.path.join(CACHE_DIR, 'data'),
    enabled=True,
    expire=60 * 60 * 24 * 35,  # 35 days
    log_file=None,
    type='dbm',
    lock_dir=os.path.join(CACHE_DIR, 'lock'),
)


class PackageNotFound(Exception):
    pass


def setup_logging(debug):
    lvl = logging.DEBUG if debug else logging.INFO
    log.setLevel(lvl)
    ch = logging.StreamHandler()
    ch.setLevel(lvl)
    formatter = logging.Formatter('%(name)s %(levelname)s %(message)s')
    ch.setFormatter(formatter)
    log.addHandler(ch)


@cache.cache('api_get')
def fetch_url(path):
    url = "http://snapshot.debian.org/%s" % path
    log.info("Fetching %s", url)
    r = requests.get(url)
    return r.json()


def fetch_last_package_list():
    r = fetch_url('mr/package/')
    return sorted(p['package'] for p in r['result'])


def fetch_package_versions(pname):
    r = fetch_url("mr/package/%s/" % pname)
    return sorted(v['version'] for v in r['result'])


def fetch_files_list(pname, ver):
    r = fetch_url("mr/package/%s/%s/srcfiles" % (pname, ver))
    print r


@cache.cache('copyright')
def fetch_copyright(path):
    log.info("Fetching %s", path)
    url = "http://metadata.ftp-master.debian.org/changelogs/main/%s" % path
    t0 = time.time()
    r = requests.get(url)
    elapsed = time.time() - t0
    padding = 1.0/MAX_QPS - elapsed  # limit QPS by sleeping a bit
    if padding >= 0:
        time.sleep(padding)

    if r.ok:
        return r.text

    # package not existing: a package could be in one archive only
    return None


def simplify_license_name(license):
    """Simplify license name. Differences between e.g. GPL-2 and GPL-2+ are
    ignored.
    """
    license = license.rstrip('+')
    return known_licenses.get(license, license)


def guess_licenses(text):
    if text.startswith('<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">'):
        raise PackageNotFound  # FIXME

    lines = text.splitlines()
    for line in lines:
        if line.startswith('License: '):
            license = line.strip().split()[-1]
            license = simplify_license_name(license)

    guessed = set([lic
                   for regex, lic in guessers
                   if re.search(regex, text)])

    if len(guessed) > 1:
        # More than one license guessed!
        if sorted(guessed) == ['Artistic', 'GPL-2']:
            return ['Perl']

        if False:  # debugging
            log.debug("---- multiple guesses: %s ----", guessed)
            log.debug(text)
            log.debug('-' * 33)
            for regex, lic in guessers:
                s = re.search(regex, text)
                if s:
                    print "%s %s %s" % (lic, regex, s.group())

        return guessed

    elif guessed:
        # A license has been guessed
        if False:  # debugging
            log.debug("---- guess: %s ----", license)
            log.debug(text)
            log.debug('-' * 33)
        return guessed

    if False:  # debugging
        log.debug("===== unknown license =====")
        log.debug(text)
        log.debug("=" * 33)
    return ['unknown']


def extract_licenses(pkg_name, text):
    """Extract license from debian/copyright if it is machine-parsable,
    otherwise perform hacky guesswork :(
    """
    try:
        c = debian.copyright.Copyright(text)
        files_selectors = {fp.files[0]: fp.license.synopsis
                           for fp in c.all_files_paragraphs()
                           if fp.license}

        # Always ignore the debian/* copyright: we care about the upstream
        files_selectors.pop(u'debian/*', None)

        if not files_selectors:
            return 'parsed', ['missing']

        licenses = set(simplify_license_name(l)
                       for l in files_selectors.itervalues())

        return 'parsed', licenses

    except debian.copyright.NotMachineReadableError:
        return 'guessed', guess_licenses(text)

    except Exception as e:
        # Unexpected exception from the copyright parser
        log.error("Parsing the copyright of %s caused %r", pkg_name, e)
        return 'guessed', guess_licenses(text)


def detect_licenses(archive, name):
    path = "%s/%s/%s_copyright" % (name[0], name, archive)
    text = fetch_copyright(path)
    if text is None:
        raise PackageNotFound()  # package not existing

    origin, licenses = extract_licenses(name, text)
    log.debug(">>>>>>>> %s: %s %r <<<<<<<<\n%s",
              name, origin, licenses, text)
    return origin, licenses


def setup_plotting():
    pd.set_option('display.mpl_style', 'default')
    pd.set_option('display.width', 5000)
    pd.set_option('display.max_columns', 60)


def write_out_summary(license_counters):
    """Write license counters to a file, in JSON format
    """
    today = date.today()
    fname = "summary_%s.json" % today.isoformat()
    with open(fname, 'w') as f:
        json.dump(license_counters, f)


def parse_args():
    ap = ArgumentParser()
    ap.add_argument('--max-packages', type=int, default=900)
    ap.add_argument('--max-licenses', type=int, default=15)
    ap.add_argument('-d', '--debug', action='store_true')
    return ap.parse_args()


def main():
    args = parse_args()
    setup_logging(args.debug)
    archive_names = ['oldstable', 'stable', 'unstable']
    license_counters = {a: Counter() for a in archive_names}

    package_names = fetch_last_package_list()

    # Extract a random, but predictable, subset of the packages
    random.seed(12345)
    random.shuffle(package_names)
    package_names = package_names[:args.max_packages]

    for archive in archive_names:
        for name in package_names:
            try:
                origin, licenses = detect_licenses(archive, name)
                license_counters[archive].update(licenses)

            except PackageNotFound:
                pass

    for a in archive_names:
        print a
        for name, num in license_counters[a].most_common(30):
            print "  %-60s %d" % (name, num)

    write_out_summary(license_counters)

    setup_plotting()
    df = pd.DataFrame(license_counters)
    df.sort(['unstable', 'stable'], ascending=[0, 0], inplace=True)
    df = df[:args.max_licenses]
    plot = df.plot(kind='bar', figsize=(20, 11))
    plot.get_figure().savefig('all.png', transparent=True)

    df['delta'] = (df.unstable - df.oldstable) / df.oldstable
    df.sort(['delta'], ascending=[0], inplace=True)
    del(df['stable'])
    del(df['unstable'])
    del(df['oldstable'])
    plot = df.plot(kind='bar', figsize=(20, 11))
    plot.get_figure().savefig('delta.png')


if __name__ == '__main__':
    try:
        main()
        print "Press Enter to exit"
        raw_input()
    finally:
        logging.shutdown()
