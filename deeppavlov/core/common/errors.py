# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Any configuration error."""
    def __init__(self, message):
        super(ConfigError, self).__init__()
        self.message = message

    def __str__(self):
        return repr(self.message)


class GpuError(Exception):
    """Any configuration error."""
    def __init__(self, message):
        super(GpuError, self).__init__()
        self.message = message

    def __str__(self):
        return repr(self.message)
