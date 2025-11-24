from tkinter import *
import tkinter.messagebox as tkMessageBox

from PIL import Image, ImageTk
import socket, threading, time, sys, traceback, os

from RtpPacket import RtpPacket

CACHE_FILE_NAME = "cache-"
CACHE_FILE_EXT = ".jpg"

class Client:
	INIT = 0
	READY = 1
	PLAYING = 2
	state = INIT
	
	SETUP = 0
	PLAY = 1
	PAUSE = 2
	TEARDOWN = 3
	

	# === CÁC HẰNG SỐ CẤU HÌNH CHO BUFFER ===
	PREBUFFER_SIZE = 10         # Số frame cần có trước khi bắt đầu phát
	PREBUFFER_TIMEOUT = 5       # Thời gian tối đa (s) để chờ pre-buffer


	# Initiation..
	def __init__(self, master, serveraddr, serverport, rtpport, filename):
		self.master = master
		self.master.protocol("WM_DELETE_WINDOW", self.handler)
		self.createWidgets()
		self.serverAddr = serveraddr
		self.serverPort = int(serverport)
		self.rtpPort = int(rtpport)
		self.fileName = filename
		self.rtspSeq = 0
		self.sessionId = 0
		self.requestSent = -1
		self.teardownAcked = 0
		self.connectToServer()


		# === CÁC BIẾN QUẢN LÝ BUFFER VÀ TRẠNG THÁI ===
		self.frameNbr = 1 # Frame video bắt đầu từ 1
		self.lastPlayedFrame = -1
		self.rtpBuffer = {}
		self.frameBuffer = {}
		self.playEvent = threading.Event()
		
	def createWidgets(self):
		"""Build GUI."""
		# Create Setup button
		self.setup = Button(self.master, width=20, padx=3, pady=3)
		self.setup["text"] = "Setup"
		self.setup["command"] = self.setupMovie
		self.setup.grid(row=1, column=0, padx=2, pady=2)
		
		# Create Play button		
		self.start = Button(self.master, width=20, padx=3, pady=3)
		self.start["text"] = "Play"
		self.start["command"] = self.playMovie
		self.start.grid(row=1, column=1, padx=2, pady=2)
		
		# Create Pause button			
		self.pause = Button(self.master, width=20, padx=3, pady=3)
		self.pause["text"] = "Pause"
		self.pause["command"] = self.pauseMovie
		self.pause.grid(row=1, column=2, padx=2, pady=2)
		
		# Create Teardown button
		self.teardown = Button(self.master, width=20, padx=3, pady=3)
		self.teardown["text"] = "Teardown"
		self.teardown["command"] =  self.exitClient
		self.teardown.grid(row=1, column=3, padx=2, pady=2)
		
		# Create a label to display the movie
		self.label = Label(self.master, height=19)
		self.label.grid(row=0, column=0, columnspan=4, sticky=W+E+N+S, padx=5, pady=5) 
	
	def setupMovie(self):
		"""Setup button handler."""
		if self.state == self.INIT:
			self.sendRtspRequest(self.SETUP)
	
	def exitClient(self):
		"""Teardown button handler."""
		self.sendRtspRequest(self.TEARDOWN)		
		self.master.destroy() # Close the gui window
		cache_file = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
		if os.path.exists(cache_file):
			os.remove(cache_file) # Delete the cache image from video

	def pauseMovie(self):
		"""Pause button handler."""
		if self.state == self.PLAYING:
			self.sendRtspRequest(self.PAUSE)
	
	def playMovie(self):
		"""Play button handler."""
		if self.state == self.READY:

			# Create a new thread to listen for RTP packets
			threading.Thread(target=self.listenRtp).start()
			self.playEvent.clear()
			self.sendRtspRequest(self.PLAY)
	
	def playFromBuffer(self):
		"""Play video from the jitter buffer"""

		while True:

			if self.playEvent.isSet():
					break
			
			if not self.frameBuffer:
				time.sleep(0.01)
				continue
			try:
				# Sắp xếp các frame trong buffer theo timestamp
				ready_frames = sorted(self.frameBuffer.keys())
				
				# Chỉ phát các frame sau frame cuối cùng đã được phát
				next_frames_to_play = [ts for ts in ready_frames if ts > self.lastPlayedFrame]
				
				if not next_frames_to_play:
					time.sleep(0.01) # Chưa có frame mới, chờ
					continue

				# Lấy frame có timestamp nhỏ nhất
				timestamp_to_play = next_frames_to_play[0]
				payload = self.frameBuffer.pop(timestamp_to_play)
				
				self.updateMovie(self.writeFrame(payload))
				self.lastPlayedFrame = timestamp_to_play

				# Điều chỉnh sleep để khớp với tốc độ video (ví dụ: 20fps ~ 50ms)
				time.sleep(0.05) 
			except Exception as e:
				print(f"Error during playback: {e}")
				pass
	
	def listenRtp(self):		
		"""Listen for RTP packets."""
		while True:
			try:
				data = self.rtpSocket.recv(20480)
				if data:
					rtpPacket = RtpPacket()
					rtpPacket.decode(data)

					seqNum = rtpPacket.seqNum()
					timestamp = rtpPacket.timestamp()
					payload = rtpPacket.getPayload()
					marker = rtpPacket.marker()

					if timestamp not in self.rtpBuffer:
						self.rtpBuffer[timestamp] = {}

					self.rtpBuffer[timestamp][seqNum] = payload

					if marker == 1:
						# Sắp xếp các payload theo đúng thứ tự seqNum
						sorted_packets = sorted(self.rtpBuffer[timestamp].items())
                    
                    	# Ghép lại
						full_frame_data = b''.join([p[1] for p in sorted_packets])
                    
                    	# Đưa frame hoàn chỉnh vào buffer phát video
                    	# Ta dùng timestamp (hoặc frameNbr nếu server gửi) làm key
						self.frameBuffer[timestamp] = full_frame_data

						del self.rtpBuffer[timestamp]
			except socket.timeout:
				pass

			except:
				# Stop listening upon requesting PAUSE or TEARDOWN
				if self.playEvent.isSet(): 
					break
				
				# Upon receiving ACK for TEARDOWN request,
				# close the RTP socket
				if self.teardownAcked == 1:
					self.rtpSocket.shutdown(socket.SHUT_RDWR)
					self.rtpSocket.close()
					break

	def startPlaybackWithPrebuffering(self):
		"""Waits for the buffer to fill up before starting playback."""

		print(f"Pre-buffering... waiting for {self.PREBUFFER_SIZE} frames.")
		startTime = time.time()

		while len(self.rtpBuffer) < self.PREBUFFER_SIZE:
			if time.time() - startTime > self.PREBUFFER_TIMEOUT or self.playEvent.isSet():
				break
			time.sleep(0.1)

		if not self.playEvent.isSet():
			print("Buffer filled. Starting playback.")
			threading.Thread(target=self.playFromBuffer).start()


	def writeFrame(self, data):
		"""Write the received frame to a temp image file. Return the image file."""
		cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
		file = open(cachename, "wb")
		file.write(data)
		file.close()
		
		return cachename
	
	def updateMovie(self, imageFile):
		"""Update the image file as video frame in the GUI."""
		photo = ImageTk.PhotoImage(Image.open(imageFile))
		self.label.configure(image = photo, height=288) 
		self.label.image = photo
		
	def connectToServer(self):
		"""Connect to the Server. Start a new RTSP/TCP session."""
		self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try:
			self.rtspSocket.connect((self.serverAddr, self.serverPort))
		except:
			tkMessageBox.showwarning('Connection Failed', 'Connection to \'%s\' failed.' %self.serverAddr)
	
	def sendRtspRequest(self, requestCode, seqNum=None):
		"""Send RTSP request to the server."""	
		
		# Setup request
		if requestCode == self.SETUP and self.state == self.INIT:
			threading.Thread(target=self.recvRtspReply).start()
			self.rtspSeq += 1
			request = f"SETUP {self.fileName} RTSP/1.0\n"
			request += f"CSeq: {self.rtspSeq}\n"
			request += f"Transport: RTP/UDP; client_port={self.rtpPort}\n" # <<< SỬA ĐỔI: Thêm \n
			self.requestSent = requestCode
		
		# Play request
		elif requestCode == self.PLAY and self.state == self.READY:
			self.rtspSeq += 1
			request = f"PLAY {self.fileName} RTSP/1.0\n"
			request += f"CSeq: {self.rtspSeq}\n"
			request += f"Session: {self.sessionId}\n"
			self.requestSent = requestCode
		
		# Pause request
		elif requestCode == self.PAUSE and self.state == self.PLAYING:
			self.rtspSeq += 1
			request = f"PAUSE {self.fileName} RTSP/1.0\n"
			request += f"CSeq: {self.rtspSeq}\n"
			request += f"Session: {self.sessionId}\n"
			self.requestSent = requestCode
			
		# Teardown request
		elif requestCode == self.TEARDOWN and not self.state == self.INIT:
			self.rtspSeq += 1
			request = f"TEARDOWN {self.fileName} RTSP/1.0\n"
			request += f"CSeq: {self.rtspSeq}\n"
			request += f"Session: {self.sessionId}\n"
			self.requestSent = requestCode

		else:
			return
		
		# Send the RTSP request using rtspSocket.
		self.rtspSocket.send(request.encode())

		print('\nData sent:\n' + request)
		
	
	def recvRtspReply(self):
		"""Receive RTSP reply from the server."""
		while True:
			reply = self.rtspSocket.recv(1024)
			
			if reply:
				self.parseRtspReply(reply.decode("utf-8"))
			
			# Close the RTSP socket upon requesting Teardown
			if self.requestSent == self.TEARDOWN:
				self.rtspSocket.shutdown(socket.SHUT_RDWR)
				self.rtspSocket.close()
				break
	
	def parseRtspReply(self, data):
		"""Parse the RTSP reply from the server."""

		lines = data.split('\n')
		seqNum = int(lines[1].split(' ')[1])
		
		# Process only if the server reply's sequence number is the same as the request's
		if seqNum == self.rtspSeq:
			session = int(lines[2].split(' ')[1])
			# New RTSP session ID
			if self.sessionId == 0:
				self.sessionId = session
			
			# Process only if the session ID is the same
			if self.sessionId == session:
				if int(lines[0].split(' ')[1]) == 200: 
					if self.requestSent == self.SETUP:
						#-------------
						# TO COMPLETE
						#-------------
						# Update RTSP state.
						self.state = self.READY
						
						# Open RTP port.
						self.openRtpPort() 
					elif self.requestSent == self.PLAY:

						self.state = self.PLAYING
						self.startPlaybackWithPrebuffering()

					elif self.requestSent == self.PAUSE:
						self.state = self.READY
						
						# The play thread exits. A new thread is created on resume.
						self.playEvent.set()
					elif self.requestSent == self.TEARDOWN:
						self.state = self.INIT
					
						# Flag the teardownAcked to close the socket.
						self.teardownAcked = 1 
	
	def openRtpPort(self):
		"""Open RTP socket binded to a specified port."""

		# Create a new datagram socket to receive RTP packets from the server
		self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

		# Set the timeout value of the socket to 0.5sec
		self.rtpSocket.settimeout(0.5)
		
		try:
			# Bind the socket to the address using the RTP port given by the client user
			self.rtpSocket.bind(('', self.rtpPort))
		except:
			tkMessageBox.showwarning('Unable to Bind', 'Unable to bind PORT=%d' %self.rtpPort)

	def handler(self):
		"""Handler on explicitly closing the GUI window."""
		self.pauseMovie()
		if tkMessageBox.askokcancel("Quit?", "Are you sure you want to quit?"):
			self.exitClient()
		else: # When the user presses cancel, resume playing.
			self.playMovie()
