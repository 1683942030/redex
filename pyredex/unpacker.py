# Copyright (c) 2016-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

import hashlib
import os
import re
import subprocess
import shutil
import zipfile

from os.path import basename, dirname, getsize, isdir, isfile, join

from pyredex.utils import abs_glob, make_temp_dir
from pyredex.log import log

class DexMetadata(object):
    def __init__(self,
                 store=None,
                 dependencies=None,
                 have_locators=False,
                 is_root_relative=False):
        self._have_locators = False
        self._store = store
        self._dependencies = dependencies
        self._have_locators = have_locators
        self._is_root_relative = is_root_relative
        self._dexen = []

    def add_dex(self, dex_path, canary_class, hash=None):
        if hash is None:
            with open(dex_path, 'rb') as dex:
                sha1hash = hashlib.sha1(dex.read()).hexdigest()
        else:
            sha1hash = hash
        self._dexen.append(
            (os.path.basename(dex_path), sha1hash, canary_class))

    def write(self, path):
        with open(path, 'w') as meta:
            if self._store is not None:
                meta.write('.id ' + self._store + '\n')
            if self._dependencies is not None:
                for dependency in self._dependencies:
                    meta.write('.requires ' + dependency + '\n')
            if self._is_root_relative:
                meta.write('.root_relative\n')
            if self._have_locators:
                meta.write('.locators\n')
            for dex in self._dexen:
                meta.write(' '.join(dex) + '\n')


class BaseDexMode(object):
    def __init__(self, dex_prefix, canary_prefix):
        self._dex_prefix = dex_prefix
        self._canary_prefix = canary_prefix

    def unpackage(self, extracted_apk_dir, dex_dir):
        primary_dex = join(extracted_apk_dir, self._dex_prefix + '.dex')
        if os.path.exists(primary_dex):
            shutil.move(primary_dex, dex_dir)

    def repackage(self, extracted_apk_dir, dex_dir, have_locators):
        primary_dex = join(dex_dir, self._dex_prefix + '.dex')
        if os.path.exists(primary_dex):
            shutil.move(primary_dex, extracted_apk_dir)

class Api21DexMode(BaseDexMode):
    """
    On API 21+, secondary dex files are in the root of the apk and are named
    classesN.dex for N in [2, 3, 4, ... ]

    Note that this mode will also be used for apps that don't have any
    secondary dex files.
    """

    def __init__(self, dex_asset_dir='assets/secondary-program-dex-jars', dex_prefix='classes', canary_prefix='secondary'):
        BaseDexMode.__init__(self, dex_prefix, canary_prefix)
        self._secondary_dir = dex_asset_dir

    def detect(self, extracted_apk_dir):
        # Note: This mode is the fallback and we only check for it after
        # checking for the other modes. This should return true for any
        # apk.
        return isfile(join(extracted_apk_dir, self._dex_prefix + '.dex'))

    def unpackage(self, extracted_apk_dir, dex_dir):
        BaseDexMode.unpackage(self, extracted_apk_dir, dex_dir)

        jar_meta_path = join(extracted_apk_dir, self._secondary_dir,
                             'metadata.txt')
        if os.path.exists(jar_meta_path):
            os.remove(jar_meta_path)
        for path in abs_glob(extracted_apk_dir, '*.dex'):
            shutil.move(path, dex_dir)

    def repackage(self, extracted_apk_dir, dex_dir, have_locators):
        BaseDexMode.repackage(self, extracted_apk_dir, dex_dir, have_locators)

        if not os.path.exists(join(extracted_apk_dir, self._secondary_dir)):
            return
        jar_meta_path = join(extracted_apk_dir,
                             self._secondary_dir,
                             'metadata.txt')
        metadata = DexMetadata(is_root_relative=True,
                               have_locators=have_locators)
        for i in range(2, 100):
            dex_path = join(dex_dir, self._dex_prefix + '%d.dex' % i)
            if not isfile(dex_path):
                break
            canary_class = self._canary_prefix + '.dex%02d.Canary' % (i - 1)
            metadata.add_dex(dex_path, canary_class)
            shutil.move(dex_path, extracted_apk_dir)
        metadata.write(jar_meta_path)

class SubdirDexMode(BaseDexMode):
    """
    `buck build katana` places secondary dexes in a subdir with no compression
    """

    def __init__(self, dex_asset_dir='assets/secondary-program-dex-jars', store_name='secondary', dex_prefix='classes', canary_prefix='secondary'):
        BaseDexMode.__init__(self, dex_prefix, canary_prefix)
        self._secondary_dir = dex_asset_dir
        self._store_name = store_name

    def detect(self, extracted_apk_dir):
        secondary_dex_dir = join(extracted_apk_dir, self._secondary_dir)
        return isdir(secondary_dex_dir) and \
                len(list(abs_glob(secondary_dex_dir, '*.dex.jar')))

    def unpackage(self, extracted_apk_dir, dex_dir):
        jars = abs_glob(join(extracted_apk_dir, self._secondary_dir),
                        '*.dex.jar')
        for jar in jars:
            dexpath = join(dex_dir, basename(jar))[:-4]
            extract_dex_from_jar(jar, dexpath)
            os.remove(jar + '.meta')
            os.remove(jar)
        os.remove(join(extracted_apk_dir, self._secondary_dir, 'metadata.txt'))
        BaseDexMode.unpackage(self, extracted_apk_dir, dex_dir)

    def repackage(self, extracted_apk_dir, dex_dir, have_locators):
        BaseDexMode.repackage(self, extracted_apk_dir, dex_dir, have_locators)

        metadata = DexMetadata(have_locators=have_locators)
        for i in range(1, 100):
            oldpath = join(dex_dir, self._dex_prefix + '%d.dex' % (i + 1))
            dexpath = join(dex_dir, self._store_name + '-%d.dex' % i)
            if not isfile(oldpath):
                break
            shutil.move(oldpath, dexpath)

            jarpath = dexpath + '.jar'
            create_dex_jar(jarpath, dexpath)
            canary_class = self._canary_prefix + '.dex%02d.Canary' % i
            metadata.add_dex(jarpath, canary_class)

            dex_meta_base = jarpath + '.meta'
            dex_meta_path = join(dex_dir, dex_meta_base)
            with open(dex_meta_path, 'w') as dex_meta:
                dex_meta.write('jar:%d dex:%d\n' %
                               (getsize(jarpath), getsize(dexpath)))

            shutil.move(dex_meta_path,
                        join(extracted_apk_dir, self._secondary_dir))
            shutil.move(jarpath, join(extracted_apk_dir,
                                      self._secondary_dir))
        jar_meta_path = join(dex_dir, 'metadata.txt')
        metadata.write(jar_meta_path)
        shutil.move(jar_meta_path, join(extracted_apk_dir, self._secondary_dir))

class XZSDexMode(BaseDexMode):
    """
    Secondary dex files are packaged in individual jar files where are then
    concatenated together and compressed with xz.

    ... This format is completely insane.
    """

    def __init__(self, dex_asset_dir='assets/secondary-program-dex-jars', store_name='secondary', dex_prefix='classes', canary_prefix='secondary'):
        BaseDexMode.__init__(self, dex_prefix, canary_prefix)
        self._xzs_dir = dex_asset_dir
        self._xzs_filename = store_name + '.dex.jar.xzs'
        self._store_name = store_name

    def detect(self, extracted_apk_dir):
        path = join(extracted_apk_dir, self._xzs_dir,
                self._xzs_filename)
        return isfile(path)

    def unpackage(self, extracted_apk_dir, dex_dir):
        src = join(extracted_apk_dir, self._xzs_dir,
                self._xzs_filename)
        dest = join(dex_dir, self._xzs_filename)

        # Move secondary dexen
        shutil.move(src, dest)

        # concat_jar is a bunch of .dex.jar files concatenated together.
        concat_jar = join(dex_dir, self._xzs_filename[:-4])
        cmd = 'cat {} | xz -d --threads 6 > {}'.format(dest, concat_jar)
        subprocess.check_call(cmd, shell=True)

        # Sizes of the concatenated .dex.jar files are stored in .meta files.
        # Read the sizes of each .dex.jar file and un-concatenate them.
        jar_size_regex = 'jar:(\d+)'
        secondary_dir = join(extracted_apk_dir, self._xzs_dir)
        jar_sizes = {}
        for i in range(1, 100):
            filename = self._store_name + '-' + str(i) + '.dex.jar.xzs.tmp~.meta'
            metadata_path = join(secondary_dir, filename)
            if isfile(metadata_path):
                with open(metadata_path) as f:
                    jar_sizes[i] = \
                            int(re.match(jar_size_regex, f.read()).group(1))
                os.remove(metadata_path)
            else:
                break

        with open(concat_jar, 'rb') as cj:
            for i in range(1, len(jar_sizes) + 1):
                jarpath = join(dex_dir, self._store_name + '-%d.dex.jar' % i)
                with open(jarpath, 'wb') as jar:
                    jar.write(cj.read(jar_sizes[i]))

        for j in jar_sizes.keys():
            assert jar_sizes[j] == getsize(dex_dir + '/' + self._store_name + '-' + str(j) + '.dex.jar')

        assert sum(jar_sizes.values()) == getsize(concat_jar)

        # Clean up everything other than dexen in the dex directory
        os.remove(concat_jar)
        os.remove(dest)

        # Lastly, unzip all the jar files and delete them
        for jarpath in abs_glob(dex_dir, '*.jar'):
            extract_dex_from_jar(jarpath, jarpath[:-4])
            os.remove(jarpath)
        BaseDexMode.unpackage(self, extracted_apk_dir, dex_dir)

    def repackage(self, extracted_apk_dir, dex_dir, have_locators):
        BaseDexMode.repackage(self, extracted_apk_dir, dex_dir, have_locators)

        dex_sizes = {}
        jar_sizes = {}

        concat_jar_path = join(dex_dir, self._store_name + '.dex.jar')
        concat_jar_meta = join(dex_dir, 'metadata.txt')
        dex_metadata = DexMetadata(have_locators=have_locators)

        with open(concat_jar_path, 'wb') as concat_jar:

            for i in range(1, 100):
                oldpath = join(dex_dir, self._dex_prefix + '%d.dex' % (i + 1))
                if not isfile(oldpath):
                    break
                dexpath = join(dex_dir, self._store_name + '-%d.dex' % i)

                # Package each dex into a jar
                shutil.move(oldpath, dexpath)
                jarpath = dexpath + '.jar'
                create_dex_jar(jarpath, dexpath)
                dex_sizes[jarpath] = getsize(dexpath)
                jar_sizes[jarpath] = getsize(jarpath)

                # Concatenate the jar files and create corresponding metadata files
                with open(jarpath + '.xzs.tmp~.meta', 'wb') as metadata:
                    sizes = 'jar:{} dex:{}'.format(
                        jar_sizes[jarpath], dex_sizes[jarpath])
                    metadata.write(bytes(sizes, 'ascii'))

                with open(jarpath, 'rb') as jar:
                    contents = jar.read()
                    concat_jar.write(contents)
                    sha1hash = hashlib.sha1(contents).hexdigest()

                canary_class = self._canary_prefix + '.dex%02d.Canary' % i
                dex_metadata.add_dex(jarpath + '.xzs.tmp~',
                                     canary_class,
                                     hash=sha1hash)

        dex_metadata.write(concat_jar_meta)

        assert getsize(concat_jar_path) == sum(getsize(x)
                for x in abs_glob(dex_dir, self._store_name + '-*.dex.jar'))

        # XZ-compress the result
        subprocess.check_call(['xz', '-z6', '--check=crc32', '--threads=6',
                concat_jar_path])

        # Copy all the archive and metadata back to the apk directory
        secondary_dex_dir = join(extracted_apk_dir, self._xzs_dir)
        for path in abs_glob(dex_dir, self._store_name + '*.meta'):
            shutil.copy(path, secondary_dex_dir)
        shutil.copy(concat_jar_meta, join(secondary_dex_dir, 'metadata.txt'))
        shutil.copy(concat_jar_path + '.xz',
                join(secondary_dex_dir, self._xzs_filename))


# These are checked in order from top to bottom. The first one to have detect()
# return true will be used.
SECONDARY_DEX_MODES = [
    XZSDexMode(),
    SubdirDexMode(),
    Api21DexMode(),
]


def detect_secondary_dex_mode(extracted_apk_dir):
    for mode in SECONDARY_DEX_MODES:
        if mode.detect(extracted_apk_dir):
            return mode
    raise Exception('Unknown secondary dex mode')


def extract_dex_from_jar(jarpath, dexpath):
    dest_directory = dirname(dexpath)
    with zipfile.ZipFile(jarpath) as jar:
        contents = jar.namelist()
        dexfiles = [name for name in contents if name.endswith('dex')]
        assert len(dexfiles) == 1, 'Expected a single dex file'
        dexname = jar.extract(dexfiles[0], dest_directory)
        os.rename(join(dest_directory, dexname), dexpath)


def create_dex_jar(jarpath, dexpath, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(jarpath, mode='w') as zf:
        zf.write(dexpath, 'classes.dex', compress_type=compression)
        zf.writestr('/META-INF/MANIFEST.MF',
                b'Manifest-Version: 1.0\n'
                b'Dex-Location: classes.dex\n'
                b'Created-By: redex\n\n')
