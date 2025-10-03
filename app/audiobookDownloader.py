import json
import os
import shutil
import subprocess
import sqlite3
import csv
import sys

# Ensure stdout is flushed immediately for Docker logging
sys.stdout.reconfigure(line_buffering=True)


config = "/config"
audiobook_download_directory = "/app"
audiobook_directory = "/audiobooks"
use_folders = True if os.getenv('AUDIOBOOK_FOLDERS') == "True" else False

con = sqlite3.connect(config + "/audiobooks.db")
with con:
	con.execute("""CREATE TABLE IF NOT EXISTS audiobooks (
				asin TEXT UNIQUE,
				title TEXT NOT NULL,
				subtitle TEXT,
				authors TEXT NOT NULL,
				series_title TEXT,
				narrators TEXT,
				series_sequence INT,
				release_date TEXT,
				downloaded INT,
				is_downloadable INT DEFAULT 1,
				restriction_reason TEXT,
				last_download_attempt TEXT
		);""")
	
	# Migrate existing table to add new columns if they don't exist
	try:
		con.execute("ALTER TABLE audiobooks ADD COLUMN is_downloadable INT DEFAULT 1")
	except sqlite3.OperationalError:
		pass  # Column already exists
	
	try:
		con.execute("ALTER TABLE audiobooks ADD COLUMN restriction_reason TEXT")
	except sqlite3.OperationalError:
		pass  # Column already exists
		
	try:
		con.execute("ALTER TABLE audiobooks ADD COLUMN last_download_attempt TEXT")
	except sqlite3.OperationalError:
		pass  # Column already exists

# program exits and errors if it can't get the activation bytes
subprocess.run(["audible", "activation-bytes"])
results = [each for each in os.listdir(config) if each.endswith('.json')]
activation_bytes = json.load(open(config + "/" + results[0]))["activation_bytes"]
if activation_bytes is None:
	print("error: no activation bytes found exiting")
	exit()



# get library from audible cli
# update db with all new titles
def update_titles():
	try:
		print("Updating library from Audible...")
		sys.stdout.flush()
		# Don't capture output so we can see progress in Docker logs
		result = subprocess.run(["audible", "library", "export"])
		if result.returncode != 0:
			print(f"Error exporting library (exit code: {result.returncode})")
			sys.stdout.flush()
			return
		
		try:
			with open("library.tsv", 'r', encoding='utf-8') as file:
				reader = csv.DictReader(file, delimiter='\t')
				cur = con.cursor()
				
				added_count = 0
				for row in reader:
					try:
						values = [row.get('asin', ''), row.get('title', ''), row.get('subtitle', ''), 
								 row.get('authors', ''), row.get('series_title', ''), row.get('narrators', ''), 
								 row.get('series_sequence', None), row.get('release_date', ''), 0, 1, None, None]
						
						if cur.execute('SELECT * FROM audiobooks WHERE asin=?', [row.get('asin', '')]).fetchone() is None:
							cur.execute('insert into audiobooks values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', values)
							print(f"Added new book to database: {row.get('title', 'Unknown')} (ASIN: {row.get('asin', 'Unknown')})")
							sys.stdout.flush()
							added_count += 1
					except Exception as e:
						print(f"Error processing row: {e}")
						sys.stdout.flush()
						continue
				
				con.commit()
				print(f"Library update complete. Added {added_count} new books.")
				sys.stdout.flush()
				
		except FileNotFoundError:
			print("Error: library.tsv file not found")
			sys.stdout.flush()
		except Exception as e:
			print(f"Error reading library file: {e}")
			sys.stdout.flush()
			
	except Exception as e:
		print(f"Error in update_titles: {e}")
		sys.stdout.flush()

def check_downloadability():
	"""Check and update downloadability status for books that haven't been checked."""
	try:
		print("🔍 Checking download restrictions for new books...")
		sys.stdout.flush()
		
		with con:
			cur = con.cursor()
			# Get books that haven't been checked for downloadability yet
			unchecked_books = cur.execute('''
				SELECT asin, title FROM audiobooks 
				WHERE is_downloadable = 1 AND restriction_reason IS NULL 
				AND downloaded = 0
				ORDER BY asin
			''').fetchall()
			
			if not unchecked_books:
				print("   All books already checked for download restrictions")
				sys.stdout.flush()
				return
			
			print(f"   Checking {len(unchecked_books)} books for download restrictions...")
			sys.stdout.flush()
			
			# Check books in batches to avoid overwhelming the API
			batch_size = 20
			for i in range(0, len(unchecked_books), batch_size):
				batch = unchecked_books[i:i+batch_size]
				asins = [book[0] for book in batch]
				asin_list = ','.join(asins)
				
				try:
					# Get detailed API response with download restriction info
					result = subprocess.run([
						"audible", "api", "1.0/library",
						"-p", f"asins={asin_list}",
						"-p", "response_groups=product_desc,product_attrs",
						"-f", "json"
					], capture_output=True, text=True)
					
					if result.returncode == 0 and result.stdout:
						api_data = json.loads(result.stdout)
						
						for item in api_data.get('items', []):
							asin = item.get('asin')
							title = item.get('title', 'Unknown')
							is_ayce = item.get('is_ayce', False)
							benefit_id = item.get('benefit_id')
							
							# Determine if book is downloadable
							is_downloadable = 1
							restriction_reason = None
							
							if is_ayce or benefit_id == "AYCL":
								is_downloadable = 0
								restriction_reason = "Audible Plus catalog book (streaming only)"
								print(f"   ⚠️  {title} (ASIN: {asin}) - {restriction_reason}")
								sys.stdout.flush()
							else:
								print(f"   ✅ {title} (ASIN: {asin}) - Downloadable")
								sys.stdout.flush()
							
							# Update database
							cur.execute('''
								UPDATE audiobooks 
								SET is_downloadable = ?, restriction_reason = ?
								WHERE asin = ?
							''', (is_downloadable, restriction_reason, asin))
					else:
						# Batch failed, try individual ASINs
						print(f"   Batch API call failed, checking individual ASINs...")
						sys.stdout.flush()
						for asin, title in batch:
							try:
								individual_result = subprocess.run([
									"audible", "api", "1.0/library",
									"-p", f"asins={asin}",
									"-p", "response_groups=product_desc,product_attrs",
									"-f", "json"
								], capture_output=True, text=True)
								
								if individual_result.returncode == 0 and individual_result.stdout:
									api_data = json.loads(individual_result.stdout)
									items = api_data.get('items', [])
									
									if items:
										item = items[0]
										is_ayce = item.get('is_ayce', False)
										benefit_id = item.get('benefit_id')
										
										if is_ayce or benefit_id == "AYCL":
											is_downloadable = 0
											restriction_reason = "Audible Plus catalog book (streaming only)"
											print(f"   ⚠️  {title} (ASIN: {asin}) - {restriction_reason}")
										else:
											is_downloadable = 1
											restriction_reason = None
											print(f"   ✅ {title} (ASIN: {asin}) - Downloadable")
										
										cur.execute('''
											UPDATE audiobooks 
											SET is_downloadable = ?, restriction_reason = ?
											WHERE asin = ?
										''', (is_downloadable, restriction_reason, asin))
									else:
										# No items returned - ASIN might be invalid or inaccessible
										print(f"   ❓ {title} (ASIN: {asin}) - No API data available")
										cur.execute('''
											UPDATE audiobooks 
											SET is_downloadable = ?, restriction_reason = ?
											WHERE asin = ?
										''', (0, "No API data available (may be invalid ASIN)", asin))
								else:
									# Individual API call also failed
									print(f"   ❌ {title} (ASIN: {asin}) - API call failed")
									cur.execute('''
										UPDATE audiobooks 
										SET is_downloadable = ?, restriction_reason = ?
										WHERE asin = ?
									''', (0, "API check failed", asin))
									
								sys.stdout.flush()
							except Exception as individual_error:
								print(f"   ❌ {title} (ASIN: {asin}) - Error: {individual_error}")
								cur.execute('''
									UPDATE audiobooks 
									SET is_downloadable = ?, restriction_reason = ?
									WHERE asin = ?
								''', (0, f"Error during check: {individual_error}", asin))
								sys.stdout.flush()
				
				except Exception as e:
					print(f"   Error checking batch starting with {batch[0][0]}: {e}")
					sys.stdout.flush()
					# Mark these as checked but unknown
					for asin, title in batch:
						cur.execute('''
							UPDATE audiobooks 
							SET restriction_reason = ?
							WHERE asin = ?
						''', ("API check failed", asin))
			
			con.commit()
			
	except Exception as e:
		print(f"Error in check_downloadability: {e}")
		sys.stdout.flush()

def run_integrity_check_and_fix():
	"""Run integrity verification with automatic fixes before downloading."""
	try:
		print("\n🔍 Running integrity verification and auto-fix...")
		sys.stdout.flush()
		
		# Run the streamlined verification script
		result = subprocess.run([
			"python", "/app/auto_integrity_check.py"
		], capture_output=True, text=True)
		
		if result.returncode >= 0:  # 0 = no issues, >0 = issues found and fixed, -1 = error
			# Show the output from the integrity check
			if result.stdout.strip():
				for line in result.stdout.strip().split('\n'):
					if line.strip():
						print(f"   {line}")
						sys.stdout.flush()
			
			if result.returncode > 0:
				print(f"✅ Integrity check fixed {result.returncode} issues")
				sys.stdout.flush()
			elif result.returncode == 0:
				print("✅ Integrity check completed - no issues found")
				sys.stdout.flush()
		else:
			print(f"⚠️  Integrity check encountered errors")
			if result.stderr:
				print(f"   Error: {result.stderr}")
			sys.stdout.flush()
			
	except Exception as e:
		print(f"⚠️  Error during integrity check: {e}")
		sys.stdout.flush()
		# Continue with downloads anyway

def create_audiobook_folder(asin):
	try:
		cur = con.cursor()
		book = cur.execute('SELECT authors, title, series_title, subtitle, narrators, series_sequence, release_date FROM audiobooks WHERE asin=?', [asin]).fetchone()
		
		if book is None:
			print(f"Warning: Book with ASIN {asin} not found in database. Skipping...")
			return None
		
		authors = book[0] or "Unknown Author"
		title = book[1] or "Unknown Title"
		series_title = book[2]
		subtitle = book[3]
		narrators = book[4] or "Unknown Narrator"
		series_sequence = book[5]
		release_date = book[6] or "Unknown"

		# Sanitize folder names by removing invalid characters
		def sanitize_name(name):
			if name is None:
				return ""
			# Remove or replace characters that aren't allowed in file paths
			invalid_chars = '<>:"/\\|?*'
			for char in invalid_chars:
				name = name.replace(char, '-')
			return name.strip()

		authors = sanitize_name(authors)
		title = sanitize_name(title)
		series_title = sanitize_name(series_title)
		subtitle = sanitize_name(subtitle)
		narrators = sanitize_name(narrators)

		directory = audiobook_directory + "/" + authors + "/"
		if series_title and series_sequence: # if series title exists the sequence also exists
			directory = directory + series_title + "/" + str(series_sequence) + " - "
		
		year = release_date.split("-")[0] if release_date and "-" in release_date else release_date
		directory = directory + year + " - " + title
		
		if subtitle:
			directory = directory + " - " + subtitle
		directory = directory + " {" + narrators + "}" + "/"

		try:
			os.makedirs(directory, exist_ok=True)
			print(f"Created/verified directory: {directory}")
		except OSError as e:
			print(f"Error creating directory {directory}: {e}")
			return None

		return directory
	
	except Exception as e:
		print(f"Error in create_audiobook_folder for ASIN {asin}: {e}")
		return None

def download_new_titles():
	cur = con.cursor()
	
	# Get downloadable books only
	downloadable_books = cur.execute('''
		SELECT asin, title, restriction_reason 
		FROM audiobooks 
		WHERE downloaded = 0 AND is_downloadable = 1
	''').fetchall()
	
	# Get and report non-downloadable books
	restricted_books = cur.execute('''
		SELECT asin, title, restriction_reason 
		FROM audiobooks 
		WHERE downloaded = 0 AND is_downloadable = 0
	''').fetchall()
	
	if restricted_books:
		print(f"\n📋 Skipping {len(restricted_books)} non-downloadable books:")
		sys.stdout.flush()
		for asin, title, reason in restricted_books:
			print(f"   ⚠️  {title} (ASIN: {asin}) - {reason or 'Unknown restriction'}")
			sys.stdout.flush()
		print()
	
	if not downloadable_books:
		print("No downloadable books found that haven't been downloaded yet.")
		sys.stdout.flush()
		return
	
	print(f"Found {len(downloadable_books)} downloadable books to process.")
	sys.stdout.flush()

	for asin, title, _ in downloadable_books:
		try:
			print(f"Downloading audiobook with ASIN: {asin}")
			sys.stdout.flush()
			
			# Update last download attempt timestamp
			from datetime import datetime
			timestamp = datetime.now().isoformat()
			cur.execute('UPDATE audiobooks SET last_download_attempt = ? WHERE asin = ?', (timestamp, asin))
			con.commit()
			
			# Don't capture output so we can see download progress in Docker logs
			result = subprocess.run(["audible", "-v", "error", "download", "-a", asin, "--aax-fallback", "--timeout", "0", "-f", "asin_ascii", "--ignore-podcasts", "-o", audiobook_download_directory])
			if result.returncode != 0:
				print(f"Download failed for ASIN {asin} (exit code: {result.returncode})")
				sys.stdout.flush()
				
				# If download consistently fails, check if it's actually not downloadable
				if result.returncode == 0:  # audible-cli returns 0 even for "not downloadable" errors
					# Try to detect "not downloadable" message in output
					try:
						# Run with captured output to check for restriction message
						check_result = subprocess.run(
							["audible", "download", "-a", asin, "--aax-fallback"], 
							capture_output=True, text=True, timeout=30
						)
						if "is not downloadable" in check_result.stdout:
							print(f"   Detected download restriction for {title} - updating database")
							cur.execute('''
								UPDATE audiobooks 
								SET is_downloadable = 0, restriction_reason = ? 
								WHERE asin = ?
							''', ("Detected as non-downloadable during download attempt", asin))
							con.commit()
							sys.stdout.flush()
					except Exception as e:
						print(f"   Could not verify download restriction: {e}")
						sys.stdout.flush()
				
				continue

			# Process downloaded files
			try:
				audiobooks = [each for each in os.listdir(audiobook_download_directory) if each.endswith(('.aax', '.aaxc'))]
			except OSError as e:
				print(f"Error accessing download directory: {e}")
				sys.stdout.flush()
				continue

			for audiobook in audiobooks:
				try:
					print(f"Processing file: {audiobook}")
					sys.stdout.flush()
					new_asin = audiobook.split("_")[0]
					asin_check = cur.execute("Select title FROM audiobooks WHERE asin=?", [new_asin]).fetchone()
					
					if asin_check is None:
						# If the ASIN doesn't exist in our database, try to use the original one
						if cur.execute("Select title FROM audiobooks WHERE asin=?", [asin]).fetchone() is not None:
							try:
								new_name = audiobook.replace(new_asin, asin)
								shutil.move(audiobook_download_directory + "/" + audiobook, audiobook_download_directory + "/" + new_name)
								audiobook = new_name
								print(f"Renamed file to match database ASIN: {new_name}")
							except OSError as e:
								print(f"Error renaming file {audiobook}: {e}")
								continue
						else:
							print(f"Warning: Cannot find ASIN {new_asin} or {asin} in database. Skipping file {audiobook}")
							continue

					current_asin = audiobook.split("_")[0]

					# Update database to mark as downloaded
					try:
						cur.execute('UPDATE audiobooks SET downloaded = 1 WHERE asin = ?', [current_asin])
						con.commit()
					except Exception as e:
						print(f"Error updating database for ASIN {current_asin}: {e}")

					src = audiobook_download_directory + "/" + audiobook
					aax_book = True if audiobook[-3:] == "aax" else False
					audiobook_name = audiobook[:-3] if aax_book else audiobook[:-4]
					
					# Check if we can create the folder structure
					try:
						folder_path = create_audiobook_folder(current_asin)
						if folder_path is None:
							print(f"Skipping {audiobook_name} - could not create folder structure")
							continue
						des = folder_path + audiobook_name + "m4b" if use_folders else audiobook_directory + "/" + audiobook_name + "m4b"
						
						# Check if destination file already exists and remove it
						if os.path.exists(des):
							print(f"Removing existing file: {os.path.basename(des)}")
							try:
								os.remove(des)
							except OSError as e:
								print(f"Warning: Could not remove existing file {des}: {e}")
								# Continue anyway - FFmpeg might be able to overwrite
						
					except Exception as e:
						print(f"Error creating folder structure for {audiobook_name}: {e}")
						continue

					# Convert the audiobook
					try:
						if aax_book:
							print(f"Converting .aax file: {audiobook_name}")
							result = subprocess.run(["ffmpeg", "-y", "-activation_bytes", activation_bytes, "-i", src, "-c", "copy", des], 
													capture_output=True, text=True)
							if result.returncode == 0:
								os.remove(src)
								print(f"Successfully converted and removed source: {audiobook_name}")
							else:
								print(f"FFmpeg conversion failed for {audiobook_name}: {result.stderr}")
						else:
							print(f"Converting .aaxc file: {audiobook_name}")
							vouchers = [each for each in os.listdir(audiobook_download_directory) if each.endswith('.voucher')]
							converted = False
							for voucher in vouchers:
								try:
									with open(audiobook_download_directory + "/" + voucher, 'r') as f:
										json_voucher = json.load(f)["content_license"]["license_response"]
									result = subprocess.run(["ffmpeg", "-y", "-audible_key", json_voucher["key"], "-audible_iv", json_voucher["iv"], "-i", src, "-c", "copy", des],
															capture_output=True, text=True)
									if result.returncode == 0:
										os.remove(src)
										os.remove(audiobook_download_directory + "/" + voucher)
										print(f"Successfully converted and cleaned up: {audiobook_name}")
										converted = True
										break
									else:
										print(f"FFmpeg conversion failed for {audiobook_name}: {result.stderr}")
								except (json.JSONDecodeError, KeyError, OSError) as e:
									print(f"Error processing voucher {voucher}: {e}")
									continue
							
							if not converted:
								print(f"Failed to convert {audiobook_name} - no valid voucher found")
					
					except Exception as e:
						print(f"Error during conversion of {audiobook_name}: {e}")
						continue

				except Exception as e:
					print(f"Error processing audiobook file {audiobook}: {e}")
					continue

		except Exception as e:
			print(f"Error processing ASIN {asin[0]}: {e}")
			continue

	# Clean up any remaining voucher files
	try:
		vouchers = [each for each in os.listdir(audiobook_download_directory) if each.endswith('.voucher')]
		for voucher in vouchers:
			try:
				os.remove(audiobook_download_directory + "/" + voucher)
				print(f"Cleaned up remaining voucher: {voucher}")
			except OSError as e:
				print(f"Error removing voucher {voucher}: {e}")
	except OSError as e:
		print(f"Error accessing download directory for cleanup: {e}")

		
def main():
	try:
		print("Starting audiobook downloader...")
		sys.stdout.flush()
		
		# First, update library from Audible
		update_titles()
		
		# Check downloadability for new books
		check_downloadability()
		
		# Run integrity verification and auto-fix before downloading
		run_integrity_check_and_fix()
		
		# Download new/fixed titles
		download_new_titles()
		
		print("Audiobook downloader cycle completed successfully.")
		sys.stdout.flush()
	except KeyboardInterrupt:
		print("Process interrupted by user.")
		sys.stdout.flush()
	except Exception as e:
		print(f"Unexpected error in main: {e}")
		print("The process will continue on the next cycle...")
		sys.stdout.flush()

if __name__ == "__main__":
	main()
