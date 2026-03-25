#!/bin/bash
sourceFolder="/Users/taniapouli/Documents/PROJECTS/Energy/GoS/WattLab_Hackathon_2025/media/"
outputFolder="/Users/taniapouli/Documents/PROJECTS/Energy/GoS/WattLab_Hackathon_2025/media/"
videoName="elfuente_180s_1080p30_prores422.mov"
blackName="black30s_1080p30.mov"
whiteName="white30s_1080p30.mov"
noiseName="noise30s_1080p30.mov"

# Set name to match time scriptXX.out
scriptOut="concat_prores_list.txt"

rm -f $scriptOut

blackFile=$sourceFolder$blackName
videoFile=$sourceFolder$videoName
whiteFile=$sourceFolder$whiteName
noiseFile=$sourceFolder$noiseName

outFile=$outputFolder"full_video_1080p30.mov"

echo file $noiseFile  >> $scriptOut
echo file $noiseFile  >> $scriptOut
echo file $blackFile  >> $scriptOut 
echo file $whiteFile  >> $scriptOut
echo file $blackFile  >> $scriptOut
echo file $videoFile  >> $scriptOut 
echo file $blackFile  >> $scriptOut

more $scriptOut


ffmpeg -f concat -safe 0 -i concat_prores_list.txt \
       -c copy \
       $outFile
