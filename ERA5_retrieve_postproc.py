#!/usr/bin/env python3

# basic python3 scripts to retrieve ERA5 data from the CDS, to replace old Bash scripts
# Parallel retrieval is done as a function of the years (set nprocs)
# A single variable at time can be retrieved
# Data are downloaded in grib and then archived in netcdf4 zip using CDO bindings
# Monthly means as well as hourly data can be downloaded
# Multiple grids are supported
# Both surface variables and pressure levels are supported.
# Support for area selection has been included
# @ Author: Paolo Davini, CNR-ISAC, Jun 2022

import os
import sys
from pathlib import Path
from cdo import Cdo
import shutil
from multiprocessing import Process
import glob

from CDS_retriever import year_retrieve, year_convert, create_filename, first_last_year, which_new_years_download
from config import parser, load_config, print_config


cdo = Cdo()


def main():

    # Call argument parser
    args = parser()

    if args.config:

        # Load YAML file
        config = load_config(args.config)

        # Print a description of the loaded configuration
        print_config(config)

        # Translate the configuration on local variables
        tmpdir = config['tmpdir']
        storedir = config['storedir']
        dataset = config['dataset']
        varlist = config['varlist']
        year1 = config['year']['begin']
        year2 = config['year']['end']
        update = config['year']['update']
        freq = config['freq']
        levelout = config['levelout']
        grid = config['grid']
        area = config['area']
        nprocs = config['nprocs']
        download_request = config['download_request']
        do_retrieve = config['do_retrieve']
        do_postproc_6h = config['do_postproc_6h']['do']
        offset_6h = config['do_postproc_6h']['offset']
        do_postproc_day = config['do_postproc_day']
        do_postproc_mon = config['do_postproc_mon']
        do_align = config['do_align']

        # if any do_postproc is true, then do_postproc is true
        do_postproc = any([do_postproc_6h, do_postproc_day, do_postproc_mon])

        # Override config with command line args
        if args.nprocs:
            print(f"Overriding YAML nprocs ({config['nprocs']}) with command-line arg ({args.nprocs})")
            nprocs = args.nprocs
        if args.update:
            print(f"Overriding YAML update ({config['year']['update']}) with command-line arg ({args.update})")
            update = args.update

        # safecheck
        if isinstance(varlist, str):
            varlist = [varlist]

        for var in varlist:

            if update:
                print("Update flag is true, detection of years...")
                year1, year2 = which_new_years_download(storedir, dataset, var, freq, grid, levelout, area)
                print(year1, year2)
                if year1 > year2:
                    print('Everything you want has been already downloaded, disabling retrieve...')
                    do_retrieve = False
                    if (freq == 'mon'):
                        print('Everything you want has been already postprocessed, disabling postproc...')
                        do_postproc = False

            # create list of years
            years = [str(i) for i in range(year1, year2+1)]

            # define the out dir and file
            savedir = Path(tmpdir, var)
            print(f'Creating directory {savedir} if it does not exist')
            Path(savedir).mkdir(parents=True, exist_ok=True)

            # retrieve block
            if do_retrieve:

                # loop on the years create the parallel process
                processes = []
                yearlist = [years[i:i + nprocs] for i in range(0, len(years), nprocs)]
                for lyears in yearlist:
                    print(f"Working on years {lyears}\n")
                    for year in lyears:
                        # print(year)
                        p = Process(target=year_retrieve, args=(dataset, var, freq, year, grid, levelout,
                                                                area, savedir, download_request))
                        p.start()
                        processes.append(p)

                    # wait for all the processes to end
                    for process in processes:
                        process.join()

            #
            if do_postproc:

                cdo.debug = True

                print('Running postproc...')
                destdir = Path(storedir, freq)
                Path(destdir).mkdir(parents=True, exist_ok=True)

                # loop on the years create the parallel process for a fast conversion
                processes = []
                yearlist = [years[i:i + nprocs] for i in range(0, len(years), nprocs)]
                for lyears in yearlist:
                    for year in lyears:
                        print('Conversion of ' + year)
                        filename = create_filename(dataset, var, freq, grid, levelout, area, year)
                        infile = Path(savedir, filename + '.grib')
                        outfile = Path(destdir, filename + '.nc')
                        
                        # if outfile exists, skip, and force is not
                        if os.path.exists(outfile):
                            print(f"Skipping {outfile} as it already exists")
                            continue
                        p = Process(target=year_convert, args=(infile, outfile))
                        # p = Process(target=cdo.copy, args=(infile, outfile, '-f nc4 -z zip'))
                        p.start()
                        processes.append(p)

                    # wait for all the processes to end
                    for process in processes:
                        process.join()

                    print('Conversion complete!')

                # extra processing for monthly data
                if freq == "mon":
                    print('Extra processing for monthly...')

                    filepattern = str(Path(destdir, create_filename(dataset, var, freq, grid, levelout, area, '????') + '.nc'))
                    first_year, last_year = first_last_year(filepattern)

                    if update:
                        # check if big file exists
                        bigfile = str(Path(destdir, create_filename(dataset, var, freq,
                                      grid, levelout, area, '????', '????') + '.nc'))
                        filebase = glob.glob(bigfile)
                        first_year, _ = first_last_year(bigfile)
                        filepattern = filebase + glob.glob(filepattern)

                    mergefile = str(Path(destdir, create_filename(dataset, var, freq, grid,
                                    levelout, area, first_year + '-' + last_year) + '.nc'))
                    print(mergefile)
                    if os.path.exists(mergefile):
                        print(f'Removing existing file {mergefile}...')
                        os.remove(mergefile)
                    print(f'Merging together into {mergefile}...')
                    cdo.cat(input=filepattern, output=mergefile, options='-f nc4 -z zip')
                    if isinstance(filepattern, str):
                        loop = glob.glob(filepattern)
                        for f in loop:
                            os.remove(f)

                    # HACK: set a common time axis for monthly data (roll back cumulated by 6hours). useful for catalog xarray loading
                    if do_align:
                        print('Aligningment required...')
                        first_time = cdo.showtime(input=f'-seltimestep,1 {mergefile}')[0]
                        if first_time != '00:00:00':
                            tempfile = str(Path(tmpdir, 'temp_align.nc'))
                            shutil.move(mergefile, tempfile)
                            cdo.shifttime('-6hours', input=tempfile, output=mergefile, options='-f nc4 -z zip')
                            os.remove(tempfile)

                # extra processing for daily data
                else:
                    print('Extra processing on hourly data')
                    
                    filepattern = Path(destdir, create_filename(dataset, var, freq, grid, levelout, area, '????') + '.nc')
                    first_year, last_year = first_last_year(filepattern)

                    time_list = []
                    if do_postproc_6h:
                        time_list.append(f'6hT0{offset_6h-1}')
                    if do_postproc_day:
                        time_list.append('day')
                    if do_postproc_mon:
                        time_list.append('mon')
                    
                    for x in time_list:
                        thedir=Path(storedir, var, x)
                        Path(thedir).mkdir(parents=True, exist_ok=True)
                        thefile=str(Path(thedir, create_filename(dataset, var, x, grid, levelout, area, first_year + '-' + last_year) + '.nc'))
                        if os.path.exists(thefile):
                            os.remove(thefile)

                        if x[0:2] == '6h':
                            cdo.timselsum(6,offset_6h, input='-cat ' + str(filepattern), 
                                output=thefile, options='-f nc4 -z zip')
                        elif x == 'day':
                            cdo.daymean(input='-cat ' + str(filepattern), 
                                output=thefile, options='-f nc4 -z zip')
                        elif x == 'mon':
                            print('Sure? why not downloading monhly data directly?')
                            cdo.monmean(input='-cat ' + str(filepattern), 
                                output=thefile, options='-f nc4 -z zip')                            
                    
    else:
        sys.exit('Error in loading the configuration!')

    return


if __name__ == "__main__":
    main()
