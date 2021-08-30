Electrum - Lightweight Ravencoin client
=====================================

::

  Licence: MIT Licence
  Author: Thomas Voegtlin
  Language: Python (>= 3.6)
  Homepage: https://electrum.org/


.. image:: https://d322cqt584bo4o.cloudfront.net/electrum/localized.svg
    :target: https://www.transifex.com/ravencoin/electrum-ravencoin/
    :alt: Help translate Electrum online




Getting started
===============

Contact us on `Discord`_!

Want to mine to hardware? See this `article`_. Trezor has similar problems. It is recommended that you set up a software wallet and mine to that, then send to your hardware from there.

Interested in a pre-built binary? Find them `here`_. Otherwise, continue reading.

Electrum Ravencoin is currently only supported for Qt-based software.

The master branch is not always guaranteed to be working as expected. If you would like to build from source or run raw python, please use the source from one of our `releases`_.

Learn how to run your own `ElectrumX Server`_.

Electrum itself is pure Python, and so are most of the required dependencies,
but not everything. The following sections describe how to run from source, but here
is a TL;DR::

    sudo apt-get install python3
    sudo apt-get install python3-pip
    sudo apt-get install python3-cryptography
    sudo apt-get install python3-pyqt5
    sudo apt-get install python3-pyqt5.qtwebengine
    sudo apt-get install libsecp256k1-0
    sudo apt-get install cmake
    sudo pip3 install virtualenv
    ./electrum-env

Not pure-python dependencies
----------------------------

If you want to use the Qt interface, install the Qt dependencies::

    sudo apt-get install python3-pyqt5

For elliptic curve operations, `libsecp256k1`_ is a required dependency::

    sudo apt-get install libsecp256k1-0

Alternatively, when running from a cloned repository, a script is provided to build
libsecp256k1 yourself::

    sudo apt-get install automake libtool
    ./contrib/make_libsecp256k1.sh

Due to the need for fast symmetric ciphers, `cryptography`_ is required.
Install from your package manager (or from pip)::

    sudo apt-get install python3-cryptography


If you would like hardware wallet support, see `this`_.

.. _libsecp256k1: https://github.com/bitcoin-core/secp256k1
.. _pycryptodomex: https://github.com/Legrandin/pycryptodome
.. _cryptography: https://github.com/pyca/cryptography
.. _this: https://github.com/spesmilo/electrum-docs/blob/master/hardware-linux.rst
.. _here: https://github.com/Electrum-RVN-SIG/electrum-ravencoin/releases
.. _Discord: https://discord.gg/VuubYncHz4
.. _article: https://support.ledger.com/hc/en-us/articles/115005165209-Receive-mining-proceeds
.. _releases: https://github.com/Electrum-RVN-SIG/electrum-ravencoin/releases
.. _`ElectrumX Server`: https://github.com/Electrum-RVN-SIG/electrumx-ravencoin

Running from tar.gz
-------------------

If you downloaded the official package (tar.gz), you can run
Electrum from its root directory without installing it on your
system; all the pure python dependencies are included in the 'packages'
directory. To run Electrum from its root directory, just do::

    ./run_electrum

You can also install Electrum on your system, by running this command::

    sudo apt-get install python3-setuptools python3-pip
    python3 -m pip install --user .

This will download and install the Python dependencies used by
Electrum instead of using the 'packages' directory.
It will also place an executable named :code:`electrum` in :code:`~/.local/bin`,
so make sure that is on your :code:`PATH` variable.


Development version (git clone)
-------------------------------

Check out the code from GitHub::

    git clone git://github.com/spesmilo/electrum.git
    cd electrum
    git submodule update --init

Run install (this should install dependencies)::

    python3 -m pip install --user -e .


Create translations (optional)::

    sudo apt-get install python-requests gettext
    ./contrib/pull_locale

Finally, to start Electrum::

    ./run_electrum



Creating Binaries
=================

Linux (tarball)
---------------

See :code:`contrib/build-linux/sdist/README.md`.


Linux (AppImage)
----------------

See :code:`contrib/build-linux/appimage/README.md`.


Mac OS X / macOS
----------------

See :code:`contrib/osx/README.md`.


Windows
-------

See :code:`contrib/build-wine/README.md`.


Android
-------

See :code:`contrib/android/Readme.md`.


Contributing
============

Any help testing the software, reporting or fixing bugs, reviewing pull requests
and recent changes, writing tests, or helping with outstanding issues is very welcome.
Implementing new features, or improving/refactoring the codebase, is of course
also welcome, but to avoid wasted effort, especially for larger changes,
we encourage discussing these on the discord first.

Besides `GitHub`_, most communication about Electrum development happens on discord, in the
:code:`#electrum-rvn-sig` channel on discord.

.. _GitHub: https://github.com/Electrum-RVN-SIG/electrum-ravencoin
