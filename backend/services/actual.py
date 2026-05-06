"""
Actual mode data provider — stub for real inspection station integration.

When APP_MODE=actual, the real inspection station workflow is:
  1. Capture 5 images and save them to disk:
       images/{batch_id}/{shoe_id}/top.jpg
       images/{batch_id}/{shoe_id}/left.jpg
       images/{batch_id}/{shoe_id}/right.jpg
       images/{batch_id}/{shoe_id}/angle_left.jpg
       images/{batch_id}/{shoe_id}/angle_right.jpg

  2. Run the AI model locally to get a prediction and confidence score.

  3. POST the result to the API:
       POST /api/shoes

The POST /api/shoes route (routes/shoes.py) handles both simulation and
actual mode — no changes needed there when switching modes.

Extend this file in a future phase to add:
  - Hardware-specific pre-processing or validation
  - Camera status checks
  - Integration with the AI model inference pipeline
"""
