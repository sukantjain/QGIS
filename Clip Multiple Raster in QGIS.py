# Script to Clip Multiple Raster in folder 
#  in QGIS Python window

# For detailed description go to the following link
# https://youtu.be/6AQi7bm13n8

# Write your output folder path with prefix 
output_path = 'C:/Users/user/Downloads/output/clipped'

root = QgsProject.instance().layerTreeRoot()
for layer in root.children():
  if layer.name().startswith('chirps'):
      params = { 'ALPHA_BAND' : True, 
      'CROP_TO_CUTLINE' : False, 
      'DATA_TYPE' : 0, 'EXTRA' : '', 
      'INPUT' : layer.name(), 
      'KEEP_RESOLUTION' : False, 
      'MASK' : 'Indian_bound', 
      'MULTITHREADING' : False, 'NODATA' : None, 'OPTIONS' : '', 
      'OUTPUT' : output_path+layer.name()+'.tif',	  
      'SET_RESOLUTION' : False, 'SOURCE_CRS' : None, 
      'TARGET_CRS' : None, 'X_RESOLUTION' : None, 
      'Y_RESOLUTION' : None }
      processing.run("gdal:cliprasterbymasklayer", params)
	  print (layer.name())
print ("task completed")