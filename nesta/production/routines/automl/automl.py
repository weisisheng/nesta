import luigi
from nesta.production.luigihacks import autobatch
from nesta.production.luigihacks import s3
from nesta.production.luigihacks.misctools import find_filepath_from_pathstub
import os
import json
import logging
import math

def get_subbucket(s3_path):
    _, s3_key = s3.parse_s3_path(s3_path)
    subbucket, _ = os.path.split(s3_key)
    return subbucket


class DummyInputTask(luigi.ExternalTask):
    s3_path_in = luigi.Parameter()

    '''Dummy task acting as the single input data source'''
    def output(self):
        '''Points to the S3 Target'''
        return s3.S3Target(self.s3_path_in)


class MLTask(autobatch.AutoBatchTask):
    name = luigi.Parameter()
    s3_path_in = luigi.Parameter()
    s3_path_out = luigi.Parameter()
    batch_size = luigi.IntParameter(default=None)
    n_batches = luigi.IntParameter(default=None)
    child = luigi.DictParameter()


    def get_input_length(self):
        f = s3.S3Target(f"{self.s3_path_in}.length").open('rb')
        total = json.load(f)
        f.close()
        return total

    def requires(self):
        logging.debug(f"{self.job_name}: Spawning MLTask with child = {self.child}")
        if len(self.child) == 0:
            return DummyInputTask(s3_path_in=self.s3_path_in)
        return MLTask(s3_path_in=self.s3_path_in, **self.child)

    def output(self):
        return s3.S3Target(self.s3_path_out)

    def prepare(self):
        if self.batch_size is None and self.n_batches is None:
            raise ValueError("Neither batch_size for n_batches set")
        
        total = self.get_input_length()
        if self.n_batches is not None:
            self.batch_size = math.ceil(total/self.n_batches)
        else:
            self.n_batches = round(total/self.batch_size) + (total % self.batch_size)

        s3_key = self.s3_path_out
        logging.debug(f"{self.job_name}: Will use {self.n_batches} to "
                      f"process the task {total} "
                      f"with batch size {self.batch_size}")

        s3fs = s3.S3FS()
        job_params = []
        n_done = 0
        for i in range(0, self.n_batches):
            first_index = i*self.batch_size
            last_index = (i+1)*self.batch_size
            if i == self.n_batches-1:
                last_index = total

            key = s3_key.replace(".json", f"-{first_index}-{last_index}-{self.test}.json")
            done = s3fs.exists(key)
            params = {"s3_path_in":self.s3_path_in,
                      "first_index": first_index,
                      "last_index": last_index,
                      "outinfo": key,
                      "done": done}
            n_done += int(done)
            job_params.append(params)
        logging.debug(f"{self.job_name}: {n_done} of {len(job_params)} "
                      "have already been done.")
        return job_params

    def combine(self, job_params):
        logging.debug(f"{self.job_name}: Combining {len(job_params)}...")
        outdata = []
        for params in job_params:
            _body = s3.S3Target(params["outinfo"]).open("rb")
            _data = _body.read().decode('utf-8')
            outdata += json.loads(_data)
        
        total = self.get_input_length()
        if len(outdata) != total:
            raise ValueError("Input and output lengths different:"
                             f"{total} in vs {len(outdata)} out")

        logging.debug(f"{self.job_name}: Writing the output (length {len(outdata)})...")
        f = self.output().open("wb")
        f.write(json.dumps(outdata).encode('utf-8'))
        f.close()

        f = s3.S3Target(f"{self.s3_path_out}.length").open("wb")
        f.write(str(len(outdata)))
        f.close()


class AutoMLTask(luigi.WrapperTask):
    s3_path_in = luigi.Parameter()
    s3_path_out = luigi.Parameter()
    s3_path_intermediate = luigi.Parameter()
    task_chain_filepath = luigi.Parameter()    

    def get_task_chain_parameters(self, env_keys=["batchable", "env_files"]):
        with open(self.task_chain_filepath) as f:
            task_chain_parameters = json.load(f)
        for row in task_chain_parameters:
            for k, v in row.items():
                if k not in env_keys:
                    continue
                if type(v) is list:
                    row[k] = [find_filepath_from_pathstub(_v) for _v in v]
                else:
                    row[k] = find_filepath_from_pathstub(v)
        return task_chain_parameters


    def prepare_task_chain_parameters(self, task_chain_parameters):
        parameters = {}
        child_params = {}
        s3_key_in = get_subbucket(self.s3_path_in)
        s3_key_out = get_subbucket(self.s3_path_out)
        
        s3_path_in = None
        for task_params in task_chain_parameters:
            if s3_path_in is None:
                s3_path_in = self.s3_path_in
            s3_path_out = (f"{self.s3_path_intermediate}"
                           f"{s3_key_in}_to_{s3_key_out}"
                           f"_{task_params['job_name']}.json")
            
            parameters = dict(name=task_params["job_name"],
                              s3_path_out=s3_path_out,
                              s3_path_in=s3_path_in,
                              child=child_params,
                              **task_params)

            s3_path_in = s3_path_out
            # "inherit" any missing parameters from the child
            for k in child_params:
                if k not in parameters:
                    parameters[k] = child_params[k]

            child_params = parameters
        parameters["s3_path_out"] = self.s3_path_out
        return parameters

    def requires(self):
        task_chain_parameters = self.get_task_chain_parameters()
        parameters = self.prepare_task_chain_parameters(task_chain_parameters)
        yield MLTask(**parameters)
