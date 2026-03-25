#!/usr/bin/env python3

from collections import OrderedDict
import subprocess as sp
import json
import sys
import re

def get_qstat_json():
    """Call qstat for JSON data"""
    qstat_output = sp.check_output(['/opt/pbs/bin/qstat','-f','-Fjson'])
    clean_qstat_output = qstat_output.replace(
            b'"Job_Name":inf,',b'"Job_Name":"Unknown",') #.replace(b'\\', b'\\\\')
    clean_qstat_output = re.sub(b'"Job_Name":\d+,', b'"Job_Name":"Unknown",', clean_qstat_output)
    clean_qstat_output = re.sub(b'"PBS_O_PATH":\S+,', b'', clean_qstat_output)
    for i in [b'expl', b'rho_low', b'rho_high']:
        clean_qstat_output = re.sub(b'"' + i + b'":[+\-]?(?:0|[1-9]\d*)(?:\.\d*)?(?:[eE][+\-]?\d+)', 
                b'"' + i + b'":"float"', clean_qstat_output)
    try:
        results = json.loads(clean_qstat_output.decode("utf-8","ignore").replace('^"^^',''),
                      object_pairs_hook=OrderedDict)
    except json.decoder.JSONDecodeError as err:
        sys.stderr.write("{0}\n".format(err))
        sys.stderr.write("Error reading queue. See que.error.log\n")
        with open("que.error.log", 'w') as f:
            for line in clean_qstat_output.decode("utf-8","ignore"):
                f.write(line)
        sys.exit(1)
    return results
    

def get_job_directory(json_data) :
    """Obtain working directory of PBS job"""
    jobDir = OrderedDict()
    if "Jobs" in json_data.keys() :
        for jobid, job in json_data["Jobs"].items():
            jobDir[jobid] = OrderedDict()
            jobDir[jobid]["Job_Name"]      = job["Job_Name"]
            jobDir[jobid]["Job Path"]      = job['Variable_List']['PBS_O_WORKDIR']
            jobDir[jobid]["Status"]        = job['job_state']
            jobDir[jobid]["Owner"]         = job['Job_Owner'].split('@')[0]
            try:
                if jobDir[jobid]["Status"] == 'R' :
                    jobDir[jobid]["CPUs"]          = job['resources_used']['ncpus']
                    if job['resources_used']['mem'][-2:] == "kb" :
                        jobMem     = float(job['resources_used']['mem'][:-2])/1024
                        if jobMem > 1024 :
                            jobMem = jobMem/1024
                            jobDir[jobid]["Memory"]     = str(round(jobMem,1))+'Gb' 
                        else :
                            jobDir[jobid]["Memory"]     = str(round(jobMem,1))+'Mb' 
                    else :         
                        jobMem     = job['resources_used']['mem']
            except:
                print ("Loading simulation details...")
    return jobDir
        
if __name__ == "__main__":
    json_data = get_qstat_json()
    #print (json_data)
    job_info = get_job_directory(json_data)
    #if job_info:
    #    for jobId, vals in job_info.items():
    #        print (jobId + "\t" + job_info[jobId]["Job_Name"] + "\t" + job_info[jobId]["Job Path"] + "\t" + str(job_info[jobId]["CPUs"]) + "\t" + job_info[jobId]["Status"] + "\t" + job_info[jobId]["Owner"] + "\t" + job_info[jobId]["Memory"])
    #else :
    #    print ("No jobs running")
    job_list = []
    for jobid, job_info in job_info.items():
        job_data = OrderedDict()
        job_data['JobID'] = jobid
        job_data['Job_Name'] = job_info['Job_Name']
        job_data['Job_Path'] = job_info['Job Path']
        job_data['Owner'] = job_info['Owner']
        job_data['Status'] = job_info['Status']
        if job_data['Status'] == 'R' :
            job_data['CPUs'] = job_info['CPUs']
            job_data['Memory'] = job_info['Memory']
        job_list.append(job_data)
    print(json.dumps(job_list))
    
    