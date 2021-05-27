#!/usr/bin/env bash

python_version=$1

case $python_version in
    3[.\d]+)
        echo "/usr/lib/python3/dist-packages/"
        exit 0
    ;;
    2\.7[.\d]*)
        echo "/usr/lib/python2.7/dist=packages/"
        exit 0
    ;;
    *)
        exit -1
    ;;
esac