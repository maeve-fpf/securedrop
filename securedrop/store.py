# -*- coding: utf-8 -*-
from pathlib import Path

import binascii
import gzip
import os
import re
import tempfile
import zipfile

from flask import current_app
from hashlib import sha256
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.utils import secure_filename

from encryption import EncryptionManager
from secure_tempfile import SecureTemporaryFile

import rm
from worker import create_queue

import typing


if typing.TYPE_CHECKING:
    # flake8 can not understand type annotation yet.
    # That is why all type annotation relative import
    # statements has to be marked as noqa.
    # http://flake8.pycqa.org/en/latest/user/error-codes.html?highlight=f401
    from typing import List, Type, Union, Optional, IO  # noqa: F401
    from tempfile import _TemporaryFileWrapper  # type: ignore # noqa: F401
    from sqlalchemy.orm import Session  # noqa: F401
    from models import Reply, Submission  # noqa: F401

_default_storage: typing.Optional["Storage"] = None


VALIDATE_FILENAME = re.compile(
    r"^(?P<index>\d+)\-[a-z0-9-_]*(?P<file_type>msg|doc\.(gz|zip)|reply)\.gpg$"
).match


class PathException(Exception):
    """An exception raised by `util.verify` when it encounters a bad path. A path
    can be bad when it is not absolute or not normalized.
    """
    pass


class TooManyFilesException(Exception):
    """An exception raised by path_without_filesystem_id when too many
    files has been found for a given submission or reply.
    This could be due to a very unlikely collision between
    journalist_designations.
    """
    pass


class NoFileFoundException(Exception):
    """An exception raised by path_without_filesystem_id when a file could
    not be found for a given submission or reply.
    This is likely due to an admin manually deleting files from the server.
    """
    pass


class NotEncrypted(Exception):
    """An exception raised if a file expected to be encrypted client-side
    is actually plaintext.
    """
    pass


def safe_renames(old: str, new: str) -> None:
    """safe_renames(old, new)

    This is a modified version of Python's os.renames that does not
    prune directories.
    Super-rename; create directories as necessary without deleting any
    left empty.  Works like rename, except creation of any intermediate
    directories needed to make the new pathname good is attempted
    first.
    Note: this function can fail with the new directory structure made
    if you lack permissions needed to unlink the leaf directory or
    file.
    """
    head, tail = os.path.split(new)
    if head and tail and not os.path.exists(head):
        os.makedirs(head)
    os.rename(old, new)


class Storage:

    def __init__(self, storage_path: str, temp_dir: str) -> None:
        if not os.path.isabs(storage_path):
            raise PathException("storage_path {} is not absolute".format(
                storage_path))
        self.__storage_path = storage_path

        if not os.path.isabs(temp_dir):
            raise PathException("temp_dir {} is not absolute".format(
                temp_dir))
        self.__temp_dir = temp_dir

        # where files and directories are sent to be securely deleted
        self.__shredder_path = os.path.abspath(os.path.join(self.__storage_path, "../shredder"))
        os.makedirs(self.__shredder_path, mode=0o700, exist_ok=True)

        # crash if we don't have a way to securely remove files
        if not rm.check_secure_delete_capability():
            raise AssertionError("Secure file deletion is not possible.")

    @classmethod
    def get_default(cls) -> "Storage":
        from sdconfig import config

        global _default_storage

        if _default_storage is None:
            _default_storage = cls(
                config.STORE_DIR,
                config.TEMP_DIR
            )

        return _default_storage

    @property
    def storage_path(self) -> str:
        return self.__storage_path

    @property
    def shredder_path(self) -> str:
        return self.__shredder_path

    def shredder_contains(self, path: str) -> bool:
        """
        Returns True if the fully-resolved path lies within the shredder.
        """
        common_path = os.path.commonpath((os.path.realpath(path), self.__shredder_path))
        return common_path == self.__shredder_path

    def store_contains(self, path: str) -> bool:
        """
        Returns True if the fully-resolved path lies within the store.
        """
        common_path = os.path.commonpath((os.path.realpath(path), self.__storage_path))
        return common_path == self.__storage_path

    def verify(self, p: str) -> bool:
        """
        Verify that a given path is valid for the store.
        """

        if self.store_contains(p):
            # verifying a hypothetical path
            if not os.path.exists(p):
                return True

            # extant paths must be directories or correctly-named plain files
            if os.path.isdir(p):
                return True

            if os.path.isfile(p) and VALIDATE_FILENAME(os.path.basename(p)):
                return True

        raise PathException("Path not valid in store: {}".format(p))

    def path(self, filesystem_id: str, filename: str = '') -> str:
        """
        Returns the path resolved within `self.__storage_path`.

        Raises PathException if `verify` doesn't like the path.
        """
        joined = os.path.join(os.path.realpath(self.__storage_path), filesystem_id, filename)
        absolute = os.path.realpath(joined)
        if not self.verify(absolute):
            raise PathException(
                """Could not resolve ("{}", "{}") to a path within the store.""".format(
                    filesystem_id, filename
                )
            )
        return absolute

    def path_without_filesystem_id(self, filename: str) -> str:
        """Get the normalized, absolute file path, within
           `self.__storage_path` for a filename when the filesystem_id
           is not known.
        """

        joined_paths = []
        for rootdir, _, files in os.walk(os.path.realpath(self.__storage_path)):
            for file_ in files:
                if file_ in filename:
                    joined_paths.append(os.path.join(rootdir, file_))

        if len(joined_paths) > 1:
            raise TooManyFilesException('Found duplicate files!')
        elif len(joined_paths) == 0:
            raise NoFileFoundException('File not found: {}'.format(filename))
        else:
            absolute = joined_paths[0]

        if not self.verify(absolute):
            raise PathException(
                """Could not resolve "{}" to a path within the store.""".format(filename)
            )
        return absolute

    def get_bulk_archive(self,
                         selected_submissions: 'List',
                         zip_directory: str = '') -> '_TemporaryFileWrapper':
        """Generate a zip file from the selected submissions"""
        zip_file = tempfile.NamedTemporaryFile(
            prefix='tmp_securedrop_bulk_dl_',
            dir=self.__temp_dir,
            delete=False)
        sources = set([i.source.journalist_designation
                       for i in selected_submissions])
        # The below nested for-loops are there to create a more usable
        # folder structure per #383
        missing_files = False

        with zipfile.ZipFile(zip_file, 'w') as zip:
            for source in sources:
                fname = ""
                submissions = [s for s in selected_submissions
                               if s.source.journalist_designation == source]
                for submission in submissions:
                    filename = self.path(submission.source.filesystem_id, submission.filename)

                    if os.path.exists(filename):
                        document_number = submission.filename.split('-')[0]
                        if zip_directory == submission.source.journalist_filename:
                            fname = zip_directory
                        else:
                            fname = os.path.join(zip_directory, source)
                        zip.write(filename, arcname=os.path.join(
                            fname,
                            "%s_%s" % (document_number,
                                       submission.source.last_updated.date()),
                            os.path.basename(filename)
                        ))
                    else:
                        missing_files = True
                        current_app.logger.error("File {} not found".format(filename))

        if missing_files:
            raise FileNotFoundError
        else:
            return zip_file

    def move_to_shredder(self, path: str) -> None:
        """
        Moves content from the store to the shredder for secure deletion.

        Python's safe_renames (and the underlying rename(2) calls) will
        silently overwrite content, which could bypass secure
        deletion, so we create a temporary directory under the
        shredder directory and move the specified content there.

        This function is intended to be atomic and quick, for use in
        deletions via the UI and API. The actual secure deletion is
        performed by an asynchronous process that monitors the
        shredder directory.
        """
        if not self.verify(path):
            raise ValueError(
                """Path is not within the store: "{}" """.format(path)
            )

        if not os.path.exists(path):
            raise ValueError(
                """Path does not exist: "{}" """.format(path)
            )

        relpath = os.path.relpath(path, start=self.storage_path)
        dest = os.path.join(tempfile.mkdtemp(dir=self.__shredder_path), relpath)
        current_app.logger.info("Moving {} to shredder: {}".format(path, dest))
        safe_renames(path, dest)

    def clear_shredder(self) -> None:
        current_app.logger.info("Clearing shredder")
        directories = []
        targets = []
        for directory, subdirs, files in os.walk(self.shredder_path):
            for subdir in subdirs:
                real_subdir = os.path.realpath(os.path.join(directory, subdir))
                if self.shredder_contains(real_subdir):
                    directories.append(real_subdir)
            for f in files:
                abs_file = os.path.abspath(os.path.join(directory, f))
                if os.path.islink(abs_file):
                    # Somehow, a symbolic link was created in the
                    # store. This shouldn't happen in normal
                    # operations. Just remove the link; don't try to
                    # shred its target. Note that we only have special
                    # handling for symlinks. Hard links -- which
                    # again, shouldn't occur in the store -- will
                    # result in the file data being shredded once for
                    # each link.
                    current_app.logger.info(
                        "Deleting link {} to {}".format(
                            abs_file, os.readlink(abs_file)
                        )
                    )
                    os.unlink(abs_file)
                    continue
                if self.shredder_contains(abs_file):
                    targets.append(abs_file)

        target_count = len(targets)
        current_app.logger.info("Files to delete: {}".format(target_count))
        for i, t in enumerate(targets, 1):
            current_app.logger.info("Securely deleting file {}/{}: {}".format(i, target_count, t))
            rm.secure_delete(t)
            current_app.logger.info("Securely deleted file {}/{}: {}".format(i, target_count, t))

        directories_to_remove = set(directories)
        dir_count = len(directories_to_remove)
        for i, d in enumerate(reversed(sorted(directories_to_remove)), 1):
            current_app.logger.debug("Removing directory {}/{}: {}".format(i, dir_count, d))
            os.rmdir(d)
            current_app.logger.debug("Removed directory {}/{}: {}".format(i, dir_count, d))

    def save_file_submission(self,
                             filesystem_id: str,
                             count: int,
                             journalist_filename: str,
                             filename: typing.Optional[str],
                             stream: 'IO[bytes]') -> str:

        if filename is not None:
            sanitized_filename = secure_filename(filename)
        else:
            sanitized_filename = secure_filename("unknown.file")

        # We store file submissions in a .gz file for two reasons:
        #
        # 1. Downloading large files over Tor is very slow. If we can
        # compress the file, we can speed up future downloads.
        #
        # 2. We want to record the original filename because it might be
        # useful, either for context about the content of the submission
        # or for figuring out which application should be used to open
        # it. However, we'd like to encrypt that info and have the
        # decrypted file automatically have the name of the original
        # file. Given various usability constraints in GPG and Tails, this
        # is the most user-friendly way we have found to do this.

        encrypted_file_name = "{0}-{1}-doc.gz.gpg".format(
            count,
            journalist_filename)
        encrypted_file_path = self.path(filesystem_id, encrypted_file_name)
        with SecureTemporaryFile("/tmp") as stf:  # nosec
            with gzip.GzipFile(filename=sanitized_filename,
                               mode='wb', fileobj=stf, mtime=0) as gzf:
                # Buffer the stream into the gzip file to avoid excessive
                # memory consumption
                while True:
                    buf = stream.read(1024 * 8)
                    if not buf:
                        break
                    gzf.write(buf)

            EncryptionManager.get_default().encrypt_source_file(
                file_in=stf,
                encrypted_file_path_out=Path(encrypted_file_path),
            )

        return encrypted_file_name

    def save_pre_encrypted_reply(self,
                                 filesystem_id: str,
                                 count: int,
                                 journalist_filename: str,
                                 content: str) -> str:
        if '-----BEGIN PGP MESSAGE-----' not in content.split('\n')[0]:
            raise NotEncrypted

        encrypted_file_name = "{0}-{1}-reply.gpg".format(count,
                                                         journalist_filename)
        encrypted_file_path = self.path(filesystem_id, encrypted_file_name)

        with open(encrypted_file_path, 'w') as fh:
            fh.write(content)

        return encrypted_file_path

    def save_message_submission(self,
                                filesystem_id: str,
                                count: int,
                                journalist_filename: str,
                                message: str) -> str:
        filename = "{0}-{1}-msg.gpg".format(count, journalist_filename)
        msg_loc = self.path(filesystem_id, filename)
        EncryptionManager.get_default().encrypt_source_message(
            message_in=message,
            encrypted_message_path_out=Path(msg_loc),
        )
        return filename


def async_add_checksum_for_file(db_obj: 'Union[Submission, Reply]', storage: Storage) -> str:
    return create_queue().enqueue(
        queued_add_checksum_for_file,
        type(db_obj),
        db_obj.id,
        storage.path(db_obj.source.filesystem_id, db_obj.filename),
        current_app.config['SQLALCHEMY_DATABASE_URI'],
    )


def queued_add_checksum_for_file(db_model: 'Union[Type[Submission], Type[Reply]]',
                                 model_id: int,
                                 file_path: str,
                                 db_uri: str) -> str:
    # we have to create our own DB session because there is no app context
    session = sessionmaker(bind=create_engine(db_uri))()
    db_obj = session.query(db_model).filter_by(id=model_id).one()
    add_checksum_for_file(session, db_obj, file_path)
    # We need to return a non-`None` value so the rq worker writes this back to Redis
    return "success"


def add_checksum_for_file(session: 'Session',
                          db_obj: 'Union[Submission, Reply]',
                          file_path: str) -> None:
    hasher = sha256()
    with open(file_path, 'rb') as f:
        while True:
            read_bytes = f.read(4096)
            if not read_bytes:
                break
            hasher.update(read_bytes)

    digest = binascii.hexlify(hasher.digest()).decode('utf-8')
    digest_str = u'sha256:' + digest
    db_obj.checksum = digest_str

    session.add(db_obj)
    session.commit()
