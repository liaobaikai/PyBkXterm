#!/bin/sh


root_path=$(cd "$(dirname "$0")";cd ..;pwd)
bin_path="${root_path}/bin"
config_path="${root_path}/conf"
config_file="${config_path}/bkserver.properties"

python_executable=$(which python3)

action=""
if [ "$1" == "" ]; then
  action="start"
else
  action="$1"
fi

$python_executable "${bin_path}/bkxtermserver.py" -D.config.file=${config_file} $action

