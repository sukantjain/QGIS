# Script to Perform Zonal Statistics over Multiple Rasters
#  in QGIS Python window

# For detailed description go to the following link
# https://youtu.be/0siupuoWGh0



root = QgsProject.instance().layerTreeRoot()
for layer in root.children():
  if layer.name().startswith('chirps'):
      prefix = layer.name()[-4:]
      params = {'INPUT_RASTER': layer.name(), 'RASTER_BAND': 1, 'INPUT_VECTOR': 'India_state_updated', 'COLUMN_PREFIX': prefix+'_', 'STATISTICS' : [2]}
      processing.run("qgis:zonalstatistics", params)
	  
print ("task completed")