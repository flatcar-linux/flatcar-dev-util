#!/usr/bin/python
#
# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import cherrypy
import multiprocessing
import os
import shutil
import tempfile

import devserver_util


class Downloader(object):
  """Download images to the devsever.

  Given a URL to a build on the archive server:

    - Determine if the build already exists.
    - Download and extract the build to a staging directory.
    - Package autotest tests.
    - Install components to static dir.
  """

  _LOG_TAG = 'DOWNLOAD'

  def __init__(self, static_dir):
    self._static_dir = static_dir
    self._build_dir = None
    self._staging_dir = None
    self._status_queue = multiprocessing.Queue()
    self._lock_tag = None

  @staticmethod
  def CanonicalizeAndParse(archive_url):
    """Canonicalize archive_url and parse it into its component parts.

    @param archive_url: a URL at which build artifacts are archived.
    @return a tuple of (canonicalized URL, build target, short build name)
    """
    archive_url = archive_url.rstrip('/')
    target, short_build = archive_url.rsplit('/', 2)[-2:]
    return archive_url, target, short_build

  @staticmethod
  def GenerateLockTag(target, short_build):
    """Generate a name for a lock scoped to this target/build pair.

    @param target: the target the build was for.
    @param short_build: short build name
    @return a name to use with AcquireLock that will scope the lock.
    """
    return '/'.join([target, short_build])

  @staticmethod
  def BuildStaged(archive_url, static_dir):
    """Returns True if the build is already staged."""
    _, target, short_build = Downloader.CanonicalizeAndParse(archive_url)
    sub_directory = Downloader.GenerateLockTag(target, short_build)
    return os.path.isdir(os.path.join(static_dir, sub_directory))

  def Download(self, archive_url, background=False):
    """Downloads the given build artifacts defined by the |archive_url|.

    If background is set to True, will return back early before all artifacts
    have been downloaded. The artifacts that can be backgrounded are all those
    that are not set as synchronous.

    TODO: refactor this into a common Download method, once unit tests are
    fixed up to make iterating on the code easier.
    """
    # Parse archive_url into target and short_build.
    # e.g. gs://chromeos-image-archive/{target}/{short_build}
    archive_url, target, short_build = self.CanonicalizeAndParse(archive_url)

    # Bind build_dir and staging_dir here so we can tell if we need to do any
    # cleanup after an exception occurs before build_dir is set.
    self._lock_tag = self.GenerateLockTag(target, short_build)

    if Downloader.BuildStaged(archive_url, self._static_dir):
      cherrypy.log('Build %s has already been processed.' % self._lock_tag,
                   self._LOG_TAG)
      self._status_queue.put('Success')
      return 'Success'

    try:
      # Create Dev Server directory for this build and tell other Downloader
      # instances we have processed this build.
      self._build_dir = devserver_util.AcquireLock(
          static_dir=self._static_dir, tag=self._lock_tag)

      self._staging_dir = tempfile.mkdtemp(suffix='_'.join([target,
                                                            short_build]))
      cherrypy.log('Gathering download requirements %s' % archive_url,
                   self._LOG_TAG)
      artifacts = self.GatherArtifactDownloads(
          self._staging_dir, archive_url, short_build, self._build_dir)
      devserver_util.PrepareBuildDirectory(self._build_dir)

      cherrypy.log('Downloading foreground artifacts from %s' % archive_url,
                   self._LOG_TAG)
      background_artifacts = []
      for artifact in artifacts:
        if artifact.Synchronous():
          artifact.Download()
          artifact.Stage()
        else:
          background_artifacts.append(artifact)

      if background:
        self._DownloadArtifactsInBackground(background_artifacts, archive_url)
      else:
        self._DownloadArtifactsSerially(background_artifacts)

    except Exception, e:
      # Release processing lock, which will remove build components directory
      # so future runs can retry.
      if self._build_dir:
        devserver_util.ReleaseLock(static_dir=self._static_dir,
                                   tag=self._lock_tag)

      self._status_queue.put(e)
      self._Cleanup()
      raise

    return 'Success'

  def _Cleanup(self):
    """Cleans up the staging dir for this downloader instanfce."""
    if self._staging_dir:
      cherrypy.log('Cleaning up staging directory %s' % self._staging_dir,
                   self._LOG_TAG)
      shutil.rmtree(self._staging_dir)

    self._staging_dir = None

  def _DownloadArtifactsSerially(self, artifacts):
    """Simple function to download all the given artifacts serially."""
    cherrypy.log('Downloading background artifacts serially.', self._LOG_TAG)
    try:
      for artifact in artifacts:
        artifact.Download()
        artifact.Stage()
    except Exception, e:
      self._status_queue.put(e)

      # Release processing lock, which will remove build components directory
      # so future runs can retry.
      if self._build_dir:
        devserver_util.ReleaseLock(static_dir=self._static_dir,
                                   tag=self._lock_tag)
    else:
      self._status_queue.put('Success')
    finally:
      self._Cleanup()

  def _DownloadArtifactsInBackground(self, artifacts, archive_url):
    """Downloads |artifacts| in the background and signals when complete."""
    proc = multiprocessing.Process(target=self._DownloadArtifactsSerially,
                                   args=(artifacts,))
    proc.start()

  def GatherArtifactDownloads(self, main_staging_dir, archive_url, short_build,
                              build_dir):
    """Wrapper around devserver_util.GatherArtifactDownloads().

    The wrapper allows mocking and overriding in derived classes.
    """
    return devserver_util.GatherArtifactDownloads(main_staging_dir, archive_url,
                                                  short_build, build_dir)

  def GetStatusOfBackgroundDownloads(self):
    """Returns the status of the background downloads.

    This commands returns the status of the background downloads and blocks
    until a status is returned.
    """
    status = self._status_queue.get()
    # In case anyone else is calling.
    self._status_queue.put(status)
    # It's possible we received an exception, if so, re-raise it here.
    if isinstance(status, Exception):
      raise status

    return status


class SymbolDownloader(Downloader):
  """Download and stage debug symbols for a build on the devsever.

  Given a URL to a build on the archive server:

    - Determine if the build already exists.
    - Download and extract the debug symbols to a staging directory.
    - Install symbols to static dir.
  """

  _DONE_FLAG = 'done'
  _LOG_TAG = 'SYMBOL_DOWNLOAD'

  @staticmethod
  def GenerateLockTag(target, short_build):
    return '/'.join([target, short_build, 'symbols'])

  def Download(self, archive_url):
    """Downloads debug symbols for the build defined by the |archive_url|.

    The symbols will be downloaded synchronously
    """
    # Parse archive_url into target and short_build.
    # e.g. gs://chromeos-image-archive/{target}/{short_build}
    archive_url, target, short_build = self.CanonicalizeAndParse(archive_url)

    # Bind build_dir and staging_dir here so we can tell if we need to do any
    # cleanup after an exception occurs before build_dir is set.
    self._lock_tag = self.GenerateLockTag(target, short_build)
    if self.SymbolsStaged(archive_url, self._static_dir):
      cherrypy.log(
          'Symbols for build %s have already been staged.' % self._lock_tag,
          self._LOG_TAG)
      return 'Success'

    try:
      # Create Dev Server directory for this build and tell other Downloader
      # instances we have processed this build.
      self._build_dir = devserver_util.AcquireLock(
          static_dir=self._static_dir, tag=self._lock_tag)

      self._staging_dir = tempfile.mkdtemp(suffix='_'.join([target,
                                                            short_build]))
      cherrypy.log('Downloading debug symbols from %s' % archive_url,
                   self._LOG_TAG)

      [symbol_artifact] = self.GatherArtifactDownloads(
          self._staging_dir, archive_url, '', self._static_dir)
      symbol_artifact.Download()
      symbol_artifact.Stage()
      self.MarkSymbolsStaged()

    except Exception:
      # Release processing "lock", which will indicate to future runs that we
      # did not succeed, and so they should try again.
      if self._build_dir:
        devserver_util.ReleaseLock(static_dir=self._static_dir,
                                   tag=self._lock_tag)
      raise
    finally:
      self._Cleanup()
    return 'Success'

  def GatherArtifactDownloads(self, temp_download_dir, archive_url, short_build,
                              static_dir):
    """Call SymbolDownloader-appropriate artifact gathering method.

    @param temp_download_dir: the tempdir into which we're downloading artifacts
                              prior to staging them.
    @param archive_url: the google storage url of the bucket where the debug
                        symbols for the desired build are stored.
    @param short_build: IGNORED
    @param staging_dir: the dir into which to stage the symbols

    @return an iterable of one DebugTarball pointing to the right debug symbols.
            This is an iterable so that it's similar to GatherArtifactDownloads.
            Also, it's possible that someday we might have more than one.
    """
    return devserver_util.GatherSymbolArtifactDownloads(temp_download_dir,
                                                        archive_url,
                                                        static_dir)

  def MarkSymbolsStaged(self):
    """Puts a flag file on disk to signal that symbols are staged."""
    with open(os.path.join(self._build_dir, self._DONE_FLAG), 'w') as flag:
      flag.write(self._DONE_FLAG)

  def SymbolsStaged(self, archive_url, static_dir):
    """Returns True if the build is already staged."""
    _, target, short_build = self.CanonicalizeAndParse(archive_url)
    sub_directory = self.GenerateLockTag(target, short_build)
    return os.path.isfile(os.path.join(static_dir,
                                       sub_directory,
                                       self._DONE_FLAG))
