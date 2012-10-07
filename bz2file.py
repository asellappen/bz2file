"""Module for reading and writing bzip2-compressed files.

This module contains a backport of Python 3.3's BZ2File class,
adapted to work with earlier versions of Python.
"""

__all__ = ["BZ2File", "open"]

__author__ = "Nadeem Vawda <nadeem.vawda@gmail.com>"

import io
import warnings

try:
    from threading import RLock
except ImportError:
    from dummy_threading import RLock

from bz2 import BZ2Compressor, BZ2Decompressor


_MODE_CLOSED   = 0
_MODE_READ     = 1
_MODE_READ_EOF = 2
_MODE_WRITE    = 3

_BUFFER_SIZE = 8192

_STR_TYPES = (str, unicode) if (str is bytes) else (str, bytes)

_builtin_open = open


class BZ2File(io.BufferedIOBase):

    """A file object providing transparent bzip2 (de)compression.

    A BZ2File can act as a wrapper for an existing file object, or refer
    directly to a named file on disk.

    Note that BZ2File provides a *binary* file interface - data read is
    returned as bytes, and data to be written should be given as bytes.
    """

    def __init__(self, filename, mode="r", buffering=None, compresslevel=9):
        """Open a bzip2-compressed file.

        If filename is a str, bytes or unicode object, it gives the name
        of the file to be opened. Otherwise, it should be a file object,
        which will be used to read or write the compressed data.

        mode can be 'r' for reading (default), 'w' for (over)writing, or 'a' for
        appending. These can equivalently be given as 'rb', 'wb', and 'ab'.

        buffering is ignored. Its use is deprecated.

        If mode is 'w' or 'a', compresslevel can be a number between 1
        and 9 specifying the level of compression: 1 produces the least
        compression, and 9 (default) produces the most compression.

        If mode is 'r', the input file may be the concatenation of
        multiple compressed streams.
        """
        # This lock must be recursive, so that BufferedIOBase's
        # readline(), readlines() and writelines() don't deadlock.
        self._lock = RLock()
        self._fp = None
        self._closefp = False
        self._mode = _MODE_CLOSED
        self._pos = 0
        self._size = -1

        if buffering is not None:
            warnings.warn("Use of 'buffering' argument is deprecated",
                          DeprecationWarning)

        if not (1 <= compresslevel <= 9):
            raise ValueError("compresslevel must be between 1 and 9")

        if mode in ("", "r", "rb"):
            mode = "rb"
            mode_code = _MODE_READ
            self._decompressor = BZ2Decompressor()
            self._buffer = None
        elif mode in ("w", "wb"):
            mode = "wb"
            mode_code = _MODE_WRITE
            self._compressor = BZ2Compressor(compresslevel)
        elif mode in ("a", "ab"):
            mode = "ab"
            mode_code = _MODE_WRITE
            self._compressor = BZ2Compressor(compresslevel)
        else:
            raise ValueError("Invalid mode: {0!r}".format(mode))

        if isinstance(filename, _STR_TYPES):
            self._fp = _builtin_open(filename, mode)
            self._closefp = True
            self._mode = mode_code
        elif hasattr(filename, "read") or hasattr(filename, "write"):
            self._fp = filename
            self._mode = mode_code
        else:
            raise TypeError("filename must be a %s or %s object, or a file" %
                            (_STR_TYPES[0].__name__, _STR_TYPES[1].__name__))

    def close(self):
        """Flush and close the file.

        May be called more than once without error. Once the file is
        closed, any other operation on it will raise a ValueError.
        """
        with self._lock:
            if self._mode == _MODE_CLOSED:
                return
            try:
                if self._mode in (_MODE_READ, _MODE_READ_EOF):
                    self._decompressor = None
                elif self._mode == _MODE_WRITE:
                    self._fp.write(self._compressor.flush())
                    self._compressor = None
            finally:
                try:
                    if self._closefp:
                        self._fp.close()
                finally:
                    self._fp = None
                    self._closefp = False
                    self._mode = _MODE_CLOSED
                    self._buffer = None

    @property
    def closed(self):
        """True if this file is closed."""
        return self._mode == _MODE_CLOSED

    def fileno(self):
        """Return the file descriptor for the underlying file."""
        self._check_not_closed()
        return self._fp.fileno()

    def seekable(self):
        """Return whether the file supports seeking."""
        if not self.readable():
            return False
        if hasattr(self._fp, "seekable"):
            return self._fp.seekable()
        else:
            return hasattr(self._fp, "seek")

    def readable(self):
        """Return whether the file was opened for reading."""
        self._check_not_closed()
        return self._mode in (_MODE_READ, _MODE_READ_EOF)

    def writable(self):
        """Return whether the file was opened for writing."""
        self._check_not_closed()
        return self._mode == _MODE_WRITE

    # Mode-checking helper functions.

    def _check_not_closed(self):
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def _check_can_read(self):
        if not self.readable():
            raise io.UnsupportedOperation("File not open for reading")

    def _check_can_write(self):
        if not self.writable():
            raise io.UnsupportedOperation("File not open for writing")

    def _check_can_seek(self):
        if not self.readable():
            raise io.UnsupportedOperation("Seeking is only supported "
                                          "on files open for reading")
        if hasattr(self._fp, "seekable") and not self._fp.seekable():
            raise io.UnsupportedOperation("The underlying file object "
                                          "does not support seeking")

    # Fill the readahead buffer if it is empty. Returns False on EOF.
    def _fill_buffer(self):
        # Depending on the input data, our call to the decompressor may not
        # return any data. In this case, try again after reading another block.
        while True:
            if self._buffer:
                return True

            if self._decompressor.unused_data:
                rawblock = self._decompressor.unused_data
                self._decompressor = BZ2Decompressor()
            else:
                rawblock = self._fp.read(_BUFFER_SIZE)

            if not rawblock:
                try:
                    self._decompressor.decompress(b"")
                except EOFError:
                    # End-of-stream marker and end of file. We're good.
                    self._mode = _MODE_READ_EOF
                    self._size = self._pos
                    return False
                else:
                    # Problem - we were expecting more compressed data.
                    raise EOFError("Compressed file ended before the "
                                   "end-of-stream marker was reached")

            try:
                self._buffer = self._decompressor.decompress(rawblock)
            except EOFError:
                # Continue to next stream.
                self._decompressor = BZ2Decompressor()
                self._buffer = self._decompressor.decompress(rawblock)

    # Read data until EOF.
    # If return_data is false, consume the data without returning it.
    def _read_all(self, return_data=True):
        blocks = []
        while self._fill_buffer():
            if return_data:
                blocks.append(self._buffer)
            self._pos += len(self._buffer)
            self._buffer = None
        if return_data:
            return b"".join(blocks)

    # Read a block of up to n bytes.
    # If return_data is false, consume the data without returning it.
    def _read_block(self, n, return_data=True):
        blocks = []
        while n > 0 and self._fill_buffer():
            if n < len(self._buffer):
                data = self._buffer[:n]
                self._buffer = self._buffer[n:]
            else:
                data = self._buffer
                self._buffer = None
            if return_data:
                blocks.append(data)
            self._pos += len(data)
            n -= len(data)
        if return_data:
            return b"".join(blocks)

    def peek(self, n=0):
        """Return buffered data without advancing the file position.

        Always returns at least one byte of data, unless at EOF.
        The exact number of bytes returned is unspecified.
        """
        with self._lock:
            self._check_can_read()
            if self._mode == _MODE_READ_EOF or not self._fill_buffer():
                return b""
            return self._buffer

    def read(self, size=-1):
        """Read up to size uncompressed bytes from the file.

        If size is negative or omitted, read until EOF is reached.
        Returns b'' if the file is already at EOF.
        """
        if size is None:
            raise TypeError()
        with self._lock:
            self._check_can_read()
            if self._mode == _MODE_READ_EOF or size == 0:
                return b""
            elif size < 0:
                return self._read_all()
            else:
                return self._read_block(size)

    def read1(self, size=-1):
        """Read up to size uncompressed bytes, while trying to avoid
        making multiple reads from the underlying stream.

        Returns b'' if the file is at EOF.
        """
        # Usually, read1() calls _fp.read() at most once. However, sometimes
        # this does not give enough data for the decompressor to make progress.
        # In this case we make multiple reads, to avoid returning b"".
        with self._lock:
            self._check_can_read()
            if (size == 0 or self._mode == _MODE_READ_EOF or
                not self._fill_buffer()):
                return b""
            if 0 < size < len(self._buffer):
                data = self._buffer[:size]
                self._buffer = self._buffer[size:]
            else:
                data = self._buffer
                self._buffer = None
            self._pos += len(data)
            return data

    def readinto(self, b):
        """Read up to len(b) bytes into b.

        Returns the number of bytes read (0 for EOF).
        """
        with self._lock:
            return io.BufferedIOBase.readinto(self, b)

    def readline(self, size=-1):
        """Read a line of uncompressed bytes from the file.

        The terminating newline (if present) is retained. If size is
        non-negative, no more than size bytes will be read (in which
        case the line may be incomplete). Returns b'' if already at EOF.
        """
        if not hasattr(size, "__index__"):
            raise TypeError("Integer argument expected")
        size = size.__index__()
        with self._lock:
            return io.BufferedIOBase.readline(self, size)

    def readlines(self, size=-1):
        """Read a list of lines of uncompressed bytes from the file.

        size can be specified to control the number of lines read: no
        further lines will be read once the total size of the lines read
        so far equals or exceeds size.
        """
        if not hasattr(size, "__index__"):
            raise TypeError("Integer argument expected")
        size = size.__index__()
        with self._lock:
            return io.BufferedIOBase.readlines(self, size)

    def write(self, data):
        """Write a byte string to the file.

        Returns the number of uncompressed bytes written, which is
        always len(data). Note that due to buffering, the file on disk
        may not reflect the data written until close() is called.
        """
        with self._lock:
            self._check_can_write()
            compressed = self._compressor.compress(data)
            self._fp.write(compressed)
            self._pos += len(data)
            return len(data)

    def writelines(self, seq):
        """Write a sequence of byte strings to the file.

        Returns the number of uncompressed bytes written.
        seq can be any iterable yielding byte strings.

        Line separators are not added between the written byte strings.
        """
        with self._lock:
            return io.BufferedIOBase.writelines(self, seq)

    # Rewind the file to the beginning of the data stream.
    def _rewind(self):
        self._fp.seek(0, 0)
        self._mode = _MODE_READ
        self._pos = 0
        self._decompressor = BZ2Decompressor()
        self._buffer = None

    def seek(self, offset, whence=0):
        """Change the file position.

        The new position is specified by offset, relative to the
        position indicated by whence. Values for whence are:

            0: start of stream (default); offset must not be negative
            1: current stream position
            2: end of stream; offset must not be positive

        Returns the new file position.

        Note that seeking is emulated, so depending on the parameters,
        this operation may be extremely slow.
        """
        with self._lock:
            self._check_can_seek()

            # Recalculate offset as an absolute file position.
            if whence == 0:
                pass
            elif whence == 1:
                offset = self._pos + offset
            elif whence == 2:
                # Seeking relative to EOF - we need to know the file's size.
                if self._size < 0:
                    self._read_all(return_data=False)
                offset = self._size + offset
            else:
                raise ValueError("Invalid value for whence: {0}".format(whence))

            # Make it so that offset is the number of bytes to skip forward.
            if offset < self._pos:
                self._rewind()
            else:
                offset -= self._pos

            # Read and discard data until we reach the desired position.
            if self._mode != _MODE_READ_EOF:
                self._read_block(offset, return_data=False)

            return self._pos

    def tell(self):
        """Return the current file position."""
        with self._lock:
            self._check_not_closed()
            return self._pos


def open(filename, mode="rb", compresslevel=9,
         encoding=None, errors=None, newline=None):
    """Open a bzip2-compressed file in binary or text mode.

    The filename argument can be an actual filename (a str, bytes or unicode
    object), or an existing file object to read from or write to.

    The mode argument can be "r", "rb", "w", "wb", "a" or "ab" for binary mode,
    or "rt", "wt" or "at" for text mode. The default mode is "rb", and the
    default compresslevel is 9.

    For binary mode, this function is equivalent to the BZ2File constructor:
    BZ2File(filename, mode, compresslevel). In this case, the encoding, errors
    and newline arguments must not be provided.

    For text mode, a BZ2File object is created, and wrapped in an
    io.TextIOWrapper instance with the specified encoding, error handling
    behavior, and line ending(s).

    """
    if "t" in mode:
        if "b" in mode:
            raise ValueError("Invalid mode: %r" % (mode,))
    else:
        if encoding is not None:
            raise ValueError("Argument 'encoding' not supported in binary mode")
        if errors is not None:
            raise ValueError("Argument 'errors' not supported in binary mode")
        if newline is not None:
            raise ValueError("Argument 'newline' not supported in binary mode")

    bz_mode = mode.replace("t", "")
    binary_file = BZ2File(filename, bz_mode, compresslevel=compresslevel)

    if "t" in mode:
        return io.TextIOWrapper(binary_file, encoding, errors, newline)
    else:
        return binary_file
