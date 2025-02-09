#!/bin/bash

set -ex

VERSION_STRING='"version": '
CURR_VERSION=$(awk -F \" '/"version": ".+"/ { print $4; exit; }' custom_components/ierc/manifest.json)
NEXT_VERSION=$1

sed -i "s/\($VERSION_STRING\).*/\1\"$NEXT_VERSION\",/" custom_components/eirc/manifest.json

