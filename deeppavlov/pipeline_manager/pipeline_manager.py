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

import os
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import as_completed
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict
from typing import Generator
from typing import List
from typing import Optional
from typing import Union

from tqdm import tqdm

from deeppavlov.core.commands.train import get_iterator_from_config
from deeppavlov.core.commands.train import read_data_by_config
from deeppavlov.core.commands.train import train_evaluate_model_from_config
from deeppavlov.core.commands.utils import expand_path
from deeppavlov.core.commands.utils import parse_config
from deeppavlov.core.common.errors import ConfigError
from deeppavlov.core.common.file import read_json
from deeppavlov.core.data.data_fitting_iterator import DataFittingIterator
from deeppavlov.core.data.data_learning_iterator import DataLearningIterator
from deeppavlov.pipeline_manager.gpu_utils import get_available_gpus
from deeppavlov.pipeline_manager.gpu_utils import gpu_free
from deeppavlov.pipeline_manager.observer import ExperimentObserver
from deeppavlov.pipeline_manager.pipelines_generator import PipeGen


class PipelineManager:
    """
    The :class:`~deeppavlov.pipeline_manager.PipelineManager` implements the functions of automatic experiment
    management. The class accepts a DeepPavlov config with structure of the experiments and additional parameters
    (class attributes) as input. Based on this information, a set of DeepPavlov configs is generated for each
    experiment. Described experiments can be run sequentially or in parallel, both on GPU and on the CPU.
    Over the course of the experiments, results, execution time and e.t.c are logged. After passing all the experiments
    a report is created in the form of a csv table. The :class:`~deeppavlov.pipeline_manager.PipelineManager` also
    supports the search for optimal hyperparameters. "grid" and "random" search modes is available.

    Running a large number of experiments especially with complex neural models may take a large amount of time.
    To avoid errors during the experiment a special test was added to check the correctness of pipelines. During the
    test, all pipelines are trained on a small part of the original dataset. All of the temporary files are saved in the
    folder “~/tmp/” and after successfully passing the test the folder is automatically deleted. If the test is failed,
    the “~/tmp/” folder with all its content remains for debugging. Test supports multiprocessing.

    Args:
        config_path: path to config file, or config dict.

    Attributes:
        exp_name: name of the experiment.
        date: date of the experiment.
        info: dict with some additional information that you want to add to the log, the content of the dictionary
              does not affect on the algorithm work. The default value is None.
        root_path: root path, the root path where the report will be generated and saved checkpoints
        save_best: boolean trigger, which determines whether to save checkpoints for all models or only for best model
        search_type: string parameter defining the type of hyperparams search, can be "grid" or "random"
        sample_num: determines the number of generated pipelines, if parameter search_type == "random"
        target_metric: The metric name on the basis of which the results will be sorted when the report
                       is generated. The default value is None, in this case the target
                       metric is taken the first name from those names that are specified in the config file.
        multiprocessing: boolean trigger, determining the running mode of the experiment.
        num_workers: upper limit on the number of workers if experiment running in multiprocessing mode
        use_gpu: may take values ["all", int, List[int], False];
                 If the parameter takes the value "all" (str) the pipeline manager automatically considers
                 all available to the user GPU (CUDA_VISIBLE_DEVICES is is taken into account). And selects as available
                 only those that meet the memory criterion. If the memory of a GPU is occupied by more than
                 "X" percent, then the GPU is considered inaccessible, and when the experiment is started,
                 the models will not start on it. For the value of the parameter "X" is responsible
                 "memory_fraction" attribute.

                 If the parameter takes the value int or List[ints] (list with numbers of GPU available for use).
                 All cards from the list are checked for availability by memory criterion. If part of the GPU are busy,
                 then only the remaining cards from the presented list will be used. If all of the presented GPU are
                 busy, an error message will appear.

                 If the parameter takes the value 'False' GPU will not be used during training.
        memory_fraction: the parameter determines the criterion of whether the gpu card is free or not.
                         If memory_fraction == 1.0 only those cards whose memory is completely free will be
                         considered as available. If memory_fraction == 0.5 cards with no more than half of the memory
                         will be considered as available.
        observer: A special class that collects auxiliary statistics and results during training, and stores
                  all the collected data in a separate log.
        pipeline_generator: A special class that generates configs for training.
        gen_len: amount of pipelines in experiment

    """

    def __init__(self, config_path: Union[str, Dict, Path]) -> None:
        """
        Initialize observer, read input args, builds a directory tree, initialize date, start test of
        experiment on tiny data.
        """
        if isinstance(config_path, (str, Path)):
            self.exp_config = read_json(config_path)
        else:
            self.exp_config = config_path

        self.exp_config = parse_config(self.exp_config)

        default_root = Path('~/.deeppavlov/experiments').resolve()

        self.date = self.exp_config['pipeline_search'].get('date', datetime.now().strftime('%Y-%m-%d'))
        self.info = self.exp_config['pipeline_search'].get('info')
        self.exp_name = self.exp_config['pipeline_search'].get('exp_name')
        self.root_path = expand_path(Path(self.exp_config['pipeline_search'].get('experiments_root', default_root)))
        self.save_best = self.exp_config['pipeline_search'].get('save_best', False)
        self.search_type = self.exp_config['pipeline_search'].get('search_type', 'random')
        self.sample_num = self.exp_config['pipeline_search'].get('sample_num', 10)
        self.target_metric = self.exp_config['pipeline_search'].get('target_metric')
        self.multiprocessing = self.exp_config['pipeline_search'].get('multiprocessing', False)
        self.num_workers = self.exp_config['pipeline_search'].get('num_workers')
        self.use_gpu = self.exp_config['pipeline_search'].get('use_gpu', False)
        self.memory_fraction = self.exp_config['pipeline_search'].get('gpu_memory_fraction', 0.99)
        self.available_gpu = None

        # create the observer
        launch_name = self.exp_config['pipeline_search'].get('launch_name', 'exp')
        self.observer = ExperimentObserver(self.exp_name, launch_name, self.root_path, self.info, self.date)
        # create the pipeline generator
        self.pipeline_generator = PipeGen(self.exp_config, self.observer.save_path, self.search_type, self.sample_num)
        self.generated_configs = [conf for conf in self.pipeline_generator()]
        self.gen_len = len(self.generated_configs)

        # write train data in observer
        self.observer.exp_info['number_of_pipes'] = self.gen_len
        self.observer.exp_info['experiment_config'] = str(config_path)

        self.add_metric_info()
        if self.use_gpu is not False:
            self.gpu_preparation()

        # write time of experiment start
        self.start_exp = time.time()
        # start test
        if self.exp_config['pipeline_search'].get('do_test', False):
            self.test()

    def add_metric_info(self) -> None:
        """ Adding metrics information in observer. """
        self.observer.exp_info['metrics'] = []
        for met in self.exp_config['train']['metrics']:
            if isinstance(met, dict):
                self.observer.exp_info['metrics'].append(met['name'])
            else:
                self.observer.exp_info['metrics'].append(met)

        if self.target_metric:
            self.observer.exp_info['target_metric'] = self.target_metric
        else:
            self.observer.exp_info['target_metric'] = self.observer.exp_info['metrics'][0]

    def gpu_preparation(self) -> None:
        """
        Calculates the number of workers and the set of available video cards, if gpu is used, based on init attributes.
        """
        try:
            cuda_vis_dev = os.environ['CUDA_VISIBLE_DEVICES']
            if cuda_vis_dev != '':
                visible_gpu = [int(q) for q in cuda_vis_dev.split(',')]
                os.environ['CUDA_VISIBLE_DEVICES'] = ""
            else:
                visible_gpu = None
        except KeyError:
            visible_gpu = None

        if isinstance(self.use_gpu, (list, int)):
            if isinstance(self.use_gpu, int):
                self.use_gpu = [self.use_gpu]

            if visible_gpu:
                self.use_gpu = list(set(self.use_gpu) & set(visible_gpu))

            if len(self.use_gpu) == 0:
                raise ValueError("GPU numbers in 'use_gpu' and 'CUDA_VISIBLE_DEVICES' "
                                 "has not intersections;")
        elif isinstance(self.use_gpu, str):
            if self.use_gpu == "all":
                self.use_gpu = visible_gpu
            else:
                raise ConfigError(f"Not supported tag for 'use_gpu' parameter: {self.use_gpu}")
        else:
            raise ConfigError("For 'use_gpu' parameter expected types: int, List[int] or 'all' tag, "
                              f"but {type(self.use_gpu)} was found.")

        self.available_gpu = get_available_gpus(gpu_select=self.use_gpu, gpu_fraction=self.memory_fraction)

        if len(self.available_gpu) == 0:
            raise ValueError(f"All selected GPU with numbers: ({set(self.use_gpu)}), are busy.")
        elif self.use_gpu and len(self.available_gpu) < len(self.use_gpu):
            print(f"PipelineManagerWarning: 'CUDA_VISIBLE_DEVICES' = ({self.use_gpu}), "
                  f"but only {self.available_gpu} are available.")

        self.num_workers = len(self.available_gpu)

    @staticmethod
    def train_pipe(pipe: Dict,
                   i: int,
                   observer_: ExperimentObserver,
                   gpu: Optional[int] = None) -> None:
        """
        Start learning single pipeline. Observer write all info in log file.

        Args:
            pipe: config dict of pipeline
            i:  number of pipeline
            observer_: link to observer object
            gpu: index of available gpu

        """
        # start pipeline time
        pipe_start = time.time()
        # modify project environment
        if gpu is not None:  # gpu can be equal 0
            os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)
        else:
            os.environ['CUDA_VISIBLE_DEVICES'] = ''

        # run pipeline train with redirected output flow
        observer_.pipe_ind = i
        observer_.pipe_conf = pipe['chainer']['pipe']
        save_path = observer_.build_pipe_checkpoint_folder(pipe, i)
        proc_out_path = save_path / f"out_{i + 1}.txt"
        proc_err_path = save_path / f"err_{i + 1}.txt"
        with proc_out_path.open("a", encoding='utf8') as out_file, proc_err_path.open("a", encoding='utf8') as err_file:
            with redirect_stderr(err_file), redirect_stdout(out_file):
                results = train_evaluate_model_from_config(pipe, to_train=True, to_validate=True)

        # add results and pipe time to log
        observer_.pipe_time = time.strftime('%H:%M:%S', time.gmtime(time.time() - pipe_start))
        observer_.pipe_res = results
        # update logger
        observer_.update_log()

    def process_pool_executor_generator(self,
                                        pipes_list: Optional[List] = None,
                                        observer: Optional[ExperimentObserver] = None,
                                        gpu: bool = False) -> Generator:
        """
        Create generator that returning tuple of args fore self.train_pipe method.

        Args:
            pipes_list:
            observer: ExperimentObserver object
            gpu: boolean trigger, determine to use gpu or not

        """
        if not pipes_list:
            pipes_list = self.generated_configs
        if not observer:
            observer = self.observer

        for i, pipe_conf in enumerate(pipes_list):
            if gpu:
                gpu_ind = self.available_gpu[i % len(self.available_gpu)]
                if not gpu_free(gpu_ind, self.memory_fraction):
                    visible_gpu = get_available_gpus(self.available_gpu, self.memory_fraction)
                    assert len(visible_gpu) != 0
                    gpu_ind = visible_gpu[0]

                yield (deepcopy(pipe_conf), i, observer, gpu_ind)
            else:
                yield (deepcopy(pipe_conf), i, observer)

    def run(self) -> None:
        """
        Run the experiment. Creates a report after the experiments.
        """
        # Start generating pipelines configs
        print(f"[ Experiment start - {self.gen_len} pipes, will be run]")
        if self.multiprocessing:
            # start multiprocessing
            with ProcessPoolExecutor(self.num_workers) as executor:
                futures = [executor.submit(self.train_pipe, *args)
                           for args in self.process_pool_executor_generator(gpu=(self.available_gpu is not None))]
                for _ in tqdm(as_completed(futures), total=len(futures)):
                    pass
        else:
            for i, pipe in enumerate(tqdm(self.generated_configs, total=self.gen_len)):
                if self.available_gpu:
                    self.train_pipe(pipe, i, self.observer, self.available_gpu[0])
                else:
                    self.train_pipe(pipe, i, self.observer)

        # save log
        self.observer.save_exp_info(time.strftime('%H:%M:%S', time.gmtime(time.time() - self.start_exp)))
        # delete all checkpoints and save only best pipe
        if self.save_best:
            self.observer.save_best_pipe()
        print("[ End of experiment ]")
        # visualization of results
        print("[ Create an experiment report ... ]")
        self.observer.build_report()
        print("[ Report created ]")

    def test(self) -> None:
        """
        Run a test experiment on a small piece of data. The test supports multiprocessing.
        """
        # create the pipeline generator
        test_observer = ExperimentObserver(self.exp_name, "test", self.root_path, self.info, self.date, True)
        test_pipe_generator = PipeGen(self.exp_config, test_observer.save_path, self.search_type, self.sample_num)
        test_pipes = [conf for conf in test_pipe_generator()]
        len_gen = len(test_pipes)

        # Start generating pipelines configs
        print(f"[ Test start - {len_gen} pipes, will be run]")
        if self.multiprocessing:
            with ProcessPoolExecutor(self.num_workers) as executor:
                futures = [executor.submit(self.test_pipe, *args)
                           for args in self.process_pool_executor_generator(test_pipes,
                                                                            test_observer,
                                                                            gpu=(self.available_gpu is not None))]
                for _ in tqdm(as_completed(futures), total=len(futures)):
                    pass
        else:
            for i, pipe in enumerate(tqdm(test_pipes, total=len_gen)):
                if self.available_gpu is not None:
                    self.test_pipe(pipe, i, test_observer, self.available_gpu[0])
                else:
                    self.test_pipe(pipe, i, test_observer)

        test_observer.del_tmp_log()
        del test_observer, test_pipe_generator, len_gen, test_pipes
        print("[ The test was successful ]")

    @staticmethod
    def test_pipe(pipe_conf: Dict, ind: int, observer_: ExperimentObserver, gpu_ind: Optional[int] = None) -> None:
        """
        Start testing single pipeline.

        Args:
            pipe_conf: pipeline config as dict
            ind: pipeline number
            gpu_ind: index of available gpu
            observer_: ExperimentObserver object

        """
        # modify project environment
        if gpu_ind:
            os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu_ind)
        else:
            os.environ['CUDA_VISIBLE_DEVICES'] = ''

        data_iterator_i = get_dataset_reader_and_iterator_for_test(pipe_conf, ind)
        results = train_evaluate_model_from_config(pipe_conf,
                                                   iterator=data_iterator_i,
                                                   to_train=True,
                                                   to_validate=False)
        observer_.pipe_res = results
        del results


def get_dataset_reader_and_iterator_for_test(config: Dict, i: int) -> Union[DataLearningIterator, DataFittingIterator]:
    """
    Creating a test iterator with small piece of train dataset. Config and data validation.

    Args:
        config: pipeline config as dict
        i: number of pipeline

    Returns:
        iterator

    """
    # create and test data generator and data iterator
    data = read_data_by_config(config)

    dataset_composition_ = {"train": False, "valid": False, "test": False}
    if i == 0:
        for dtype in dataset_composition_.keys():
            if len(data.get(dtype, [])) != 0:
                dataset_composition_[dtype] = True
    else:
        for dtype in dataset_composition_.keys():
            if len(data.get(dtype, [])) == 0 and dataset_composition_[dtype]:
                raise ConfigError(f"The file structure in the {config['dataset_reader']['data_path']} dataset differs "
                                  "from the rest datasets.")

    iterator = get_iterator_from_config(config, data)

    # get a tiny data from dataset
    if len(iterator.data['train']) <= 100:
        print("!!!!!!!!!!!!! WARNING !!!!!!!!!!!!! Length of 'train' part dataset <= 100. "
              "Please check the dataset_iterator config")
        tiny_train = iterator.data['train']
    else:
        tiny_train = iterator.data['train'][:100]
    iterator.train = tiny_train

    if len(iterator.data['valid']) <= 20:
        tiny_valid = iterator.data['valid']
    else:
        tiny_valid = iterator.data['valid'][:20]
    iterator.valid = tiny_valid

    if len(iterator.data['test']) <= 20:
        tiny_test = iterator.data['test']
    else:
        tiny_test = iterator.data['test'][:20]
    iterator.test = tiny_test

    iterator.data = {'train': tiny_train,
                     'valid': tiny_valid,
                     'test': tiny_test,
                     'all': tiny_train + tiny_valid + tiny_test}

    return iterator
