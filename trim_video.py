import cv2
import os

def trim_video(input_path, output_path, end_time_sec):
    if not os.path.exists(input_path):
        print(f"Error: {input_path} not found.")
        return

    cap = cv2.VideoCapture(input_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    
    # Define codec and create VideoWriter
    # Using 'mp4v' for MP4 output
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    max_frames = int(end_time_sec * fps)
    current_frame = 0

    print(f"Trimming {input_path} to {output_path}...")
    print(f"Target duration: {end_time_sec}s ({max_frames} frames)")

    while cap.isOpened() and current_frame < max_frames:
        ret, frame = cap.read()
        if not ret:
            break
        
        out.write(frame)
        current_frame += 1
        
        if current_frame % 100 == 0:
            print(f"Processed {current_frame}/{max_frames} frames...")

    cap.release()
    out.release()
    print(f"Done! Saved to {output_path}")

if __name__ == "__main__":
    trim_video('test_bot.mp4', 'test_bot_cutted.mp4', 58)
